from __future__ import annotations

import csv
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from . import __version__
from .transformation import as_feature_collection
from .validation import candidate_profile_errors, schema_provenance


def merge(existing: list[dict[str, Any]], candidate: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Upsert submitted records without treating an omitted record as deleted."""
    merged = {x["project_id"]: x for x in existing}
    merged.update({x["project_id"]: x for x in candidate})
    return [merged[k] for k in sorted(merged)]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_csv(records: list[dict[str, Any]], path: Path) -> None:
    keys = sorted({key for record in records for key in record if key != "geometry"})
    fields = (["project_id"] if "project_id" in keys else []) + [key for key in keys if key != "project_id"] + ["geometry"]
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for record in sorted(records, key=lambda item: item["project_id"]):
            row = {key: record.get(key) for key in fields if key != "geometry"}
            row["geometry"] = json.dumps(record.get("geometry"), sort_keys=True, separators=(",", ":"))
            writer.writerow(row)


def _write_geopackage(records: list[dict[str, Any]], path: Path) -> None:
    import geopandas as gpd
    from shapely.geometry import shape

    # OGR's GeoPackage writer does not support Python list columns. Preserve
    # multivalued LinkML slots as deterministic JSON strings in that format.
    rows = [{key: json.dumps(value, sort_keys=True, separators=(",", ":")) if isinstance(value, list) else value for key, value in record.items() if key != "geometry"} for record in records]
    geometry = [shape(record["geometry"]) for record in records]
    gpd.GeoDataFrame(rows, geometry=geometry, crs="EPSG:3310").to_file(path, driver="GPKG", layer="projects", index=False)


def _validate_snapshot(records: list[dict[str, Any]], directory: Path) -> None:
    """Validate public contracts and spatial output before making it current."""
    import geopandas as gpd

    expected_ids = [record["project_id"] for record in sorted(records, key=lambda item: item["project_id"])]
    for name in ("projects.geojson", "projects.gpkg"):
        frame = gpd.read_file(directory / name)
        if frame.crs is None or frame.crs.to_epsg() != 3310:
            raise ValueError(f"{name} is not EPSG:3310")
        if frame.geometry.is_empty.any() or not frame.geometry.is_valid.all():
            raise ValueError(f"{name} contains invalid geometry")
        if name.endswith(".geojson"):
            payload = json.loads((directory / name).read_text(encoding="utf-8"))
            produced = [{**feature["properties"], "geometry": feature["geometry"]} for feature in payload["features"]]
        else:
            produced = []
            for properties, geometry in zip(frame.drop(columns="geometry").to_dict("records"), frame.geometry):
                restored = {key: json.loads(value) if isinstance(value, str) and value.startswith("[") else value for key, value in properties.items()}
                produced.append({**restored, "geometry": geometry.__geo_interface__})
        if [record["project_id"] for record in produced] != expected_ids:
            raise ValueError(f"{name} project ordering differs from the snapshot")
        errors = candidate_profile_errors(produced, "RestorationProjectPublicRecord")
        if errors:
            raise ValueError(f"{name} violates the public LinkML profile: {errors[0][1]}")
    csv_rows = list(csv.DictReader((directory / "projects.csv").open(encoding="utf-8", newline="")))
    if [row["project_id"] for row in csv_rows] != expected_ids:
        raise ValueError("projects.csv project ordering differs from the snapshot")


def publish_local(public_records: list[dict[str, Any]], root: Path, version: str, metadata: dict[str, Any] | None = None) -> Path:
    """Create a validated immutable public snapshot, then replace ``current.json``."""
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
        (staging / "projects.geojson").write_text(json.dumps(as_feature_collection(ordered), sort_keys=True, separators=(",", ":")), encoding="utf-8")
        _write_geopackage(ordered, staging / "projects.gpkg")
        _write_csv(ordered, staging / "projects.csv")
        _validate_snapshot(ordered, staging)
        checksums = {name: _sha256(staging / name) for name in ("projects.geojson", "projects.gpkg", "projects.csv")}
        snapshot_metadata = {**extra, "snapshot_version": version, "schema_version": extra.get("schema_version", schema_provenance()["version"]), "pipeline_version": __version__, "output_checksums": checksums}
        (staging / "metadata.json").write_text(json.dumps(snapshot_metadata, indent=2, sort_keys=True), encoding="utf-8")
        staging.replace(target)
        pointer = {"snapshot_version": version, "schema_version": snapshot_metadata["schema_version"], "pipeline_version": __version__, "artifacts": {name: f"{version}/{name}" for name in ("projects.geojson", "projects.gpkg", "projects.csv", "metadata.json")}, "output_checksums": checksums}
        pointer_tmp = root / ".current.json.tmp"
        pointer_tmp.write_text(json.dumps(pointer, indent=2, sort_keys=True), encoding="utf-8")
        pointer_tmp.replace(root / "current.json")
        return target
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
