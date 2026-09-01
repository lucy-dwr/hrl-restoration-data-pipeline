from __future__ import annotations

import csv
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from . import __version__
from .transformation import public_feature_collection
from .validation import (
    EXPECTED_WGS84_BOUNDS,
    PUBLIC_GEOJSON_CRS,
    candidate_profile_errors,
    multivalued_slots,
    positions_outside,
    schema_provenance,
)

_UNSET = object()

# Documented download convention (submission_serialization: semicolon_delimited):
# GeoJSON keeps multivalued slots as arrays; GeoPackage and CSV join them with
# "; ". The map's own conversion scripts use the same delimiter.
_MULTIVALUE_JOIN = "; "
_MULTIVALUE_SPLIT = ";"


def _flatten(value: Any) -> Any:
    """Join a multivalued list into the "; "-delimited download form."""
    if isinstance(value, list):
        return _MULTIVALUE_JOIN.join(str(item) for item in value) if value else None
    return value


def merge(existing: list[dict[str, Any]], candidate: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Upsert submitted records without treating an omitted record as deleted."""
    merged = {x["project_id"]: x for x in existing}
    merged.update({x["project_id"]: x for x in candidate})
    return [merged[k] for k in sorted(merged)]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_csv(records: list[dict[str, Any]], path: Path) -> None:
    """Attributes only: no geometry column. Multivalued slots joined with "; "."""
    keys = sorted({key for record in records for key in record if key != "geometry"})
    fields = (["project_id"] if "project_id" in keys else []) + [key for key in keys if key != "project_id"]
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        for record in sorted(records, key=lambda item: item["project_id"]):
            writer.writerow({key: _flatten(record.get(key)) for key in fields})


def _write_geopackage(records: list[dict[str, Any]], path: Path) -> None:
    import geopandas as gpd
    from shapely.geometry import shape

    # GeoPackage columns are scalar; multivalued LinkML slots become "; "-joined
    # strings, matching the GeoJSON->GPKG conversion the map ships. Give every
    # row the same keys (None where a field is absent) so the writer never has
    # to reconcile a ragged frame.
    keys = sorted({key for record in records for key in record if key != "geometry"})
    rows = [{key: _flatten(record.get(key)) for key in keys} for record in records]
    geometry = [shape(record["geometry"]) for record in records]
    gpd.GeoDataFrame(rows, geometry=geometry, crs="EPSG:3310").to_file(path, driver="GPKG", layer="projects", index=False)


def _restore_row(properties: dict[str, Any], geometry: Any, multivalued: set[str]) -> dict[str, Any]:
    """Rebuild a record from a GeoPackage or CSV row.

    An unset GeoPackage cell reads back as a float NaN and a CSV cell as "";
    both mean "field absent". Multivalued slots are split back to a list.
    """
    record: dict[str, Any] = {}
    for key, value in properties.items():
        if value is None or value == "" or (isinstance(value, float) and value != value):
            continue
        record[key] = (
            [part.strip() for part in str(value).split(_MULTIVALUE_SPLIT) if part.strip()]
            if key in multivalued
            else value
        )
    record["geometry"] = geometry
    return record


def _validate_snapshot(records: list[dict[str, Any]], directory: Path) -> None:
    """Validate public contracts and spatial output before making it current."""
    import geopandas as gpd
    from shapely.geometry import shape

    ordered = sorted(records, key=lambda item: item["project_id"])
    expected_ids = [record["project_id"] for record in ordered]
    multivalued = multivalued_slots("RestorationProjectPublicRecord")

    # GeoJSON is the faithful representation: full LinkML validation happens here.
    # It is read with json rather than gpd.read_file because OGR drops array
    # columns (the multivalued slots) on read.
    payload = json.loads((directory / "projects.geojson").read_text(encoding="utf-8"))
    if payload.get("crs", {}).get("properties", {}).get("name") != PUBLIC_GEOJSON_CRS:
        raise ValueError("projects.geojson is not WGS84 lon/lat (CRS84)")
    geojson_records = [{**feature["properties"], "geometry": feature["geometry"]} for feature in payload["features"]]
    if [record["project_id"] for record in geojson_records] != expected_ids:
        raise ValueError("projects.geojson project ordering differs from the snapshot")
    for record in geojson_records:
        geometry = shape(record["geometry"])
        if geometry.is_empty or not geometry.is_valid:
            raise ValueError("projects.geojson contains invalid geometry")
        outside = positions_outside(record["geometry"], EXPECTED_WGS84_BOUNDS)
        if outside:
            lon, lat = outside[0]
            raise ValueError(
                f"projects.geojson geometry for {record['project_id']} is outside the expected "
                f"lon/lat extent {EXPECTED_WGS84_BOUNDS} (first outlier {lon:.5f}, {lat:.5f}); "
                "the snapshot is not in WGS84 lon/lat"
            )
    errors = candidate_profile_errors(geojson_records, "RestorationProjectPublicRecord")
    if errors:
        raise ValueError(f"projects.geojson violates the public LinkML profile: {errors[0][1]}")

    gpkg_frame = gpd.read_file(directory / "projects.gpkg")
    if gpkg_frame.crs is None or gpkg_frame.crs.to_epsg() != 3310:
        raise ValueError("projects.gpkg is not EPSG:3310")
    if gpkg_frame.geometry.is_empty.any() or not gpkg_frame.geometry.is_valid.all():
        raise ValueError("projects.gpkg contains invalid geometry")

    # GeoPackage and CSV are "; "-delimited views of the same records: check they
    # round-trip to the GeoJSON records rather than re-running LinkML on a lossy
    # serialization.
    reference = {record["project_id"]: {k: v for k, v in record.items() if k != "geometry"} for record in geojson_records}

    gpkg_records = [
        _restore_row(props, geom.__geo_interface__, multivalued)
        for props, geom in zip(gpkg_frame.drop(columns="geometry").to_dict("records"), gpkg_frame.geometry)
    ]

    with (directory / "projects.csv").open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if "geometry" in (reader.fieldnames or []):
            raise ValueError("projects.csv must not contain a geometry column")
        csv_records = [_restore_row(row, None, multivalued) for row in reader]

    for name, produced in (("projects.gpkg", gpkg_records), ("projects.csv", csv_records)):
        if [record["project_id"] for record in produced] != expected_ids:
            raise ValueError(f"{name} project ordering differs from the snapshot")
        for record in produced:
            want = reference[record["project_id"]]
            for key in multivalued:
                if want.get(key, []) != record.get(key, []):
                    raise ValueError(f"{name} does not round-trip {key} for {record['project_id']}")


def _pointer_checksum(root: Path) -> str | None:
    pointer = root / "current.json"
    return _sha256(pointer) if pointer.is_file() else None


def activate_local_snapshot(root: Path, version: str, expected_pointer_checksum: str | None | object = _UNSET) -> None:
    """Make an existing immutable snapshot current, optionally conditionally.

    ``expected_pointer_checksum`` is captured before promotion starts.  It
    prevents a second local promotion from silently replacing a pointer that
    changed while the first promotion was preparing its snapshot.
    """
    target = root / version
    metadata_path = target / "metadata.json"
    if not target.is_dir() or not metadata_path.is_file():
        raise ValueError("immutable snapshot is incomplete")
    if expected_pointer_checksum is not _UNSET and _pointer_checksum(root) != expected_pointer_checksum:
        raise ValueError("current.json changed during promotion")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    checksums = metadata.get("output_checksums")
    if not isinstance(checksums, dict) or any(
        not isinstance(name, str) or not isinstance(checksum, str) or not (target / name).is_file()
        or _sha256(target / name) != checksum
        for name, checksum in checksums.items()
    ):
        raise ValueError("immutable snapshot checksums do not match")
    pointer = {
        "snapshot_version": version,
        "schema_version": metadata["schema_version"],
        "pipeline_version": metadata["pipeline_version"],
        "artifacts": {name: f"{version}/{name}" for name in ("projects.geojson", "projects.gpkg", "projects.csv", "metadata.json")},
        "output_checksums": checksums,
    }
    pointer_tmp = root / ".current.json.tmp"
    pointer_tmp.write_text(json.dumps(pointer, indent=2, sort_keys=True), encoding="utf-8")
    pointer_tmp.replace(root / "current.json")


def publish_local(
    public_records: list[dict[str, Any]], root: Path, version: str,
    metadata: dict[str, Any] | None = None, *, update_pointer: bool = True,
    expected_pointer_checksum: str | None | object = _UNSET,
) -> Path:
    """Create a validated immutable public snapshot and, by default, make it current."""
    root.mkdir(parents=True, exist_ok=True)
    target = root / version
    if target.exists():
        raise FileExistsError("immutable snapshot already exists")
    staging = root / f".{version}.staging"
    if staging.exists():
        raise FileExistsError("publication staging directory already exists")
    ordered = sorted(public_records, key=lambda item: item["project_id"])
    extra = dict(metadata or {})
    try:
        staging.mkdir()
        (staging / "projects.geojson").write_text(json.dumps(public_feature_collection(ordered), sort_keys=True, separators=(",", ":")), encoding="utf-8")
        _write_geopackage(ordered, staging / "projects.gpkg")
        _write_csv(ordered, staging / "projects.csv")
        _validate_snapshot(ordered, staging)
        checksums = {name: _sha256(staging / name) for name in ("projects.geojson", "projects.gpkg", "projects.csv")}
        snapshot_metadata = {**extra, "snapshot_version": version, "schema_version": extra.get("schema_version", schema_provenance()["version"]), "pipeline_version": __version__, "artifact_crs": {"projects.geojson": "EPSG:4326", "projects.gpkg": "EPSG:3310"}, "output_checksums": checksums}
        (staging / "metadata.json").write_text(json.dumps(snapshot_metadata, indent=2, sort_keys=True), encoding="utf-8")
        staging.replace(target)
        if update_pointer:
            activate_local_snapshot(root, version, expected_pointer_checksum)
        return target
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
