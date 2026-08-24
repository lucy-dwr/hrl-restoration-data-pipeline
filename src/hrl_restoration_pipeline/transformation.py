from __future__ import annotations
from typing import Any

PUBLIC_EXCLUDED = {"contact_name", "contact_email", "contractors", "funding_secured", "funding_gap", "estimated_budget_comments", "construction_completion_year_comments", "source_project_id", "source_organization_code", "last_submission_id", "source_data_as_of", "update_date"}

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
    return [{k: v for k, v in record.items() if k not in PUBLIC_EXCLUDED} for record in records]

def as_feature_collection(records: list[dict[str, Any]]) -> dict[str, Any]:
    # Candidate and local-publication geometries have been reprojected during
    # ingestion. Include the explicit CRS member for offline GIS consumers.
    return {"type": "FeatureCollection", "crs": {"type": "name", "properties": {"name": "EPSG:3310"}}, "features": [{"type": "Feature", "properties": {k: v for k, v in record.items() if k != "geometry"}, "geometry": record.get("geometry")} for record in records]}
