from __future__ import annotations
from typing import Any
from .validation import SCHEMA_PATH
from linkml_runtime.utils.schemaview import SchemaView

def canonicalize(records: list[dict[str, Any]], manifest: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for record in records:
        value = dict(record)
        budget, secured, supplied_gap = value.get("estimated_budget"), value.get("funding_secured"), value.get("funding_gap")
        value.update({"funding_gap": budget - secured if isinstance(budget, (int, float)) and isinstance(secured, (int, float)) else supplied_gap,
                      "source_organization_code": manifest["organization_code"], "last_submission_id": manifest["submission_id"],
                      "source_data_as_of": manifest["data_as_of"], "record_status": "active", "update_date": manifest["data_as_of"]})
        out.append(value)
    return sorted(out, key=lambda x: x["project_id"])

def publicize(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    public_fields = {slot.name for slot in SchemaView(str(SCHEMA_PATH)).class_induced_slots("RestorationProjectPublicRecord")}
    public_fields.add("geometry")
    return [
        {key: value for key, value in record.items() if key in public_fields}
        for record in records
        if record.get("record_status") == "active"
    ]

def as_feature_collection(records: list[dict[str, Any]]) -> dict[str, Any]:
    # Candidate and local-publication geometries have been reprojected during
    # ingestion. Include the explicit CRS member for offline GIS consumers.
    return {"type": "FeatureCollection", "crs": {"type": "name", "properties": {"name": "EPSG:3310"}}, "features": [{"type": "Feature", "properties": {k: v for k, v in record.items() if k != "geometry"}, "geometry": record.get("geometry")} for record in records]}
