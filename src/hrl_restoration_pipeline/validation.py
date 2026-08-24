from __future__ import annotations

import json
import subprocess
import tempfile
from datetime import datetime, timezone
from copy import deepcopy
from pathlib import Path
from typing import Any

from linkml_runtime.utils.schemaview import SchemaView

from . import __version__
from .models import Report, Repair
from .registry import ProjectIdRegistry

SCHEMA_PATH = Path(__file__).parents[2] / "schema-snapshots/hrl-restoration-schema/v1.2.0/hrl_restoration_project.yaml"
MANIFEST_PATH = SCHEMA_PATH.with_name("manifest.json")
ALLOWED = {"Polygon", "MultiPolygon", "Point", "MultiPoint"}
SHAPEFILE_FIELD_ALIASES = {
    "project_na": "project_name", "project_de": "project_description", "project_st": "project_stage",
    "lead_entit": "lead_entity", "early_impl": "early_implementation", "project_ty": "project_type",
    "target_spe": "target_species", "constructi": "construction_start_year", "estimated_": "estimated_budget",
    "funding_se": "funding_secured", "funding_ga": "funding_gap",
}


def schema_provenance() -> dict[str, str]:
    manifest = json.loads(MANIFEST_PATH.read_text())
    return {"name": "hrl-restoration-schema", "version": manifest["tag"], "checksum": manifest["artifacts"][SCHEMA_PATH.name]}


def _normalize(record: dict[str, Any], report: Report) -> dict[str, Any]:
    result = deepcopy(record); rid = str(record.get("project_id") or "<missing>")
    for key, value in list(result.items()):
        if isinstance(value, str):
            trimmed = value.strip()
            if trimmed != value:
                result[key] = trimmed
                report.repairs.append(Repair(rid, key, value, trimmed, "trim_whitespace", __version__))
        if key in {"project_stage", "lead_entity", "project_type", "target_species", "contractors", "funding_sources"} and isinstance(result.get(key), str):
            old = result[key]; new = [x.strip() for x in old.split(";") if x.strip()]
            result[key] = new
            report.repairs.append(Repair(rid, key, old, new, "semicolon_to_list", __version__))
    return result


def validate_linkml_profile(record: dict[str, Any], class_name: str) -> str | None:
    """Delegate structural validation to the installed LinkML validator, not a duplicate model."""
    payload = dict(record)
    payload["geometry"] = "spatial-feature-column"  # geometry is represented outside the attribute table.
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as stream:
        json.dump(payload, stream); stream.flush()
        completed = subprocess.run(
            ["linkml-validate", "--schema", str(SCHEMA_PATH), "--target-class", class_name, stream.name],
            capture_output=True, text=True, check=False,
        )
    if completed.returncode:
        return (completed.stdout + completed.stderr).strip().replace("\n", " ") or "record violates LinkML schema"
    return None


def candidate_profile_errors(records: list[dict[str, Any]], class_name: str) -> list[tuple[str, str]]:
    """Validate generated candidate records against their distinct LinkML profile."""
    errors = []
    for record in records:
        error = validate_linkml_profile(record, class_name)
        if error: errors.append((str(record.get("project_id", "<missing>")), error))
    return errors


def validate_records(records: list[dict[str, Any]], registry: ProjectIdRegistry, manifest: dict[str, Any], spatial: list[dict[str, Any]] | None = None, validation_timestamp: str | None = None) -> tuple[list[dict[str, Any]], Report]:
    timestamp = validation_timestamp or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    report = Report(manifest.get("submission_id"), registry.provenance(), schema_provenance(), __version__, timestamp)
    normalized = [_normalize(r, report) for r in records]
    # LinkML-derived requiredness: SchemaView reads required flags from the pinned source.
    view = SchemaView(str(SCHEMA_PATH))
    slots = view.class_induced_slots("RestorationProjectSubmission")
    required = {s.name for s in slots if s.required}
    valid_values = {enum.name: set(enum.permissible_values) for enum in view.all_enums().values()}
    enum_slots = {s.name: s.range for s in slots if s.range in valid_values}
    seen: set[str] = set()
    for index, record in enumerate(normalized):
        rid = str(record.get("project_id") or f"row-{index + 1}")
        native_error = validate_linkml_profile(record, "RestorationProjectSubmission")
        if native_error: report.add("linkml", "ERROR", "native_linkml_validation", native_error, rid)
        for key in required:
            if key == "geometry": continue
            if record.get(key) in (None, "", []): report.add("linkml", "ERROR", "required_slot", f"required field {key} is missing", rid)
        pid = record.get("project_id")
        if not pid or not registry.eligible(str(pid)): report.add("business", "ERROR", "eligible_project_id", "project_id is missing, unknown, or not eligible", rid)
        elif pid in seen: report.add("business", "ERROR", "unique_project_id", "duplicate project_id in submission", rid)
        seen.add(str(pid))
        for slot, enum_name in enum_slots.items():
            values = record.get(slot, [])
            values = values if isinstance(values, list) else [values]
            for value in values:
                if value not in valid_values[enum_name]: report.add("controlled_vocabulary", "ERROR", "permissible_value", f"{slot} has unknown value {value!r}", rid)
        stages = set(record.get("project_stage") or [])
        if not record.get("contact_name") or not record.get("contact_email"): report.add("business", "WARNING", "contact_details", "contact details are absent", rid)
        construction = bool(stages & {"construction", "post-construction monitoring and science"})
        for key in ("estimated_budget", "funding_secured", "construction_start_year", "construction_completion_year"):
            if construction and record.get(key) in (None, ""): report.add("business", "ERROR", "stage_requiredness", f"{key} is required at construction or post-construction", rid)
            elif not construction and record.get(key) in (None, ""): report.add("business", "WARNING", "stage_requiredness", f"{key} is not yet supplied", rid)
        types = set(record.get("project_type") or [])
        exempt = types and types <= {"fish screen installation or improvement", "fish passage improvement"}
        if not exempt and record.get("acreage") in (None, ""): report.add("business", "ERROR", "acreage_required", "acreage is required except for exclusively fish screen/passage work", rid)
        supplied_gap = record.get("funding_gap")
        if supplied_gap not in (None, ""):
            budget, secured = record.get("estimated_budget"), record.get("funding_secured")
            if not isinstance(supplied_gap, (int, float)):
                report.add("business", "ERROR", "funding_gap_type", "supplied funding_gap must be numeric", rid)
            elif isinstance(budget, (int, float)) and isinstance(secured, (int, float)) and supplied_gap != budget - secured:
                report.add("business", "WARNING", "funding_gap_recalculated", "supplied funding_gap differs from the deterministic calculation and will be replaced", rid)
            elif not (isinstance(budget, (int, float)) and isinstance(secured, (int, float))):
                report.add("business", "WARNING", "funding_gap_passthrough", "supplied funding_gap is retained because it cannot be recalculated", rid)
    if spatial:
        _validate_spatial(spatial, report)
    return normalized, report


def _validate_spatial(features: list[dict[str, Any]], report: Report) -> None:
    from shapely.geometry import shape
    for item in features:
        rid = str(item.get("project_id") or "<missing>")
        if not item.get("crs"): report.add("spatial", "ERROR", "crs_required", "input CRS is required", rid)
        geometry = item.get("geometry") or {}; kind = geometry.get("type")
        if kind not in ALLOWED: report.add("spatial", "ERROR", "geometry_type", f"geometry type {kind!r} is not permitted", rid)
        if not geometry.get("coordinates"): report.add("spatial", "ERROR", "nonempty_geometry", "geometry is empty", rid)
        elif kind in ALLOWED:
            try:
                if not shape(geometry).is_valid: report.add("spatial", "ERROR", "valid_geometry", "geometry is invalid", rid)
            except (TypeError, ValueError): report.add("spatial", "ERROR", "valid_geometry", "geometry cannot be parsed", rid)
        if item.get("reprojected"):
            report.repairs.append(Repair(rid, "geometry.crs", item["source_crs"], "EPSG:3310", "reproject_to_epsg_3310", __version__))


def read_spatial(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read once into a working representation; source files are never altered."""
    import geopandas as gpd
    try:
        if path.suffix.lower() == ".gpkg":
            import pyogrio
            if len(pyogrio.list_layers(path)) != 1: raise ValueError("GeoPackage must contain exactly one spatial layer")
        frame = gpd.read_file(path)
    except Exception as exc:
        raise ValueError(f"unreadable or corrupt spatial input: {path.name}") from exc
    # DBF field names in a zipped shapefile are limited to ten characters.
    # These deterministic aliases are only an ingestion compatibility layer;
    # the LinkML schema remains the validation contract.
    if path.suffix.lower() == ".zip":
        frame = frame.rename(columns={column: SHAPEFILE_FIELD_ALIASES.get(column, column) for column in frame.columns})
    if frame.crs is None:
        properties = frame.drop(columns="geometry").to_dict("records")
        return [{**r, "geometry": g.__geo_interface__} for r, g in zip(properties, frame.geometry)], [{**r, "geometry": g.__geo_interface__, "crs": None, "source_crs": None, "reprojected": False} for r, g in zip(properties, frame.geometry)]
    try: reprojected = frame.to_crs(3310)
    except Exception as exc: raise ValueError(f"cannot safely reproject spatial input to EPSG:3310: {path.name}") from exc
    records, spatial = [], []
    for original, geometry in zip(frame.drop(columns="geometry").to_dict("records"), reprojected.geometry):
        records.append({**original, "geometry": geometry.__geo_interface__}); spatial.append({**original, "geometry": geometry.__geo_interface__, "crs": "EPSG:3310", "source_crs": str(frame.crs), "reprojected": str(frame.crs).upper() not in {"EPSG:3310", "3310"}})
    return records, spatial
