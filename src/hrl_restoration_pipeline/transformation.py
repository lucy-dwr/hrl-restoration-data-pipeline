from __future__ import annotations
from typing import Any
from .validation import PUBLIC_EPSG, PUBLIC_GEOJSON_CRS, SCHEMA_PATH, WORKING_EPSG, reproject_geometry
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
    # Internal working form: canonical storage and the candidate GeoJSON. Geometry
    # is in the equal-area working CRS (EPSG:3310), reprojected during ingestion;
    # the explicit CRS member keeps it unambiguous for offline GIS consumers.
    return {"type": "FeatureCollection", "crs": {"type": "name", "properties": {"name": f"EPSG:{WORKING_EPSG}"}}, "features": [{"type": "Feature", "properties": {k: v for k, v in record.items() if k != "geometry"}, "geometry": record.get("geometry")} for record in records]}


def public_feature_collection(records: list[dict[str, Any]]) -> dict[str, Any]:
    """WGS84 lon/lat FeatureCollection (RFC 7946) for the public map and downloads.

    Internal candidate and canonical GeoJSON stay in the equal-area working CRS;
    only this published form is reprojected to lon/lat, which is what web maps
    and the GeoJSON RFC require. The GeoPackage download stays in the working CRS.
    """
    features = []
    for record in records:
        geometry = record.get("geometry")
        features.append({
            "type": "Feature",
            "properties": {k: v for k, v in record.items() if k != "geometry"},
            "geometry": reproject_geometry(geometry, WORKING_EPSG, PUBLIC_EPSG) if geometry else None,
        })
    return {"type": "FeatureCollection", "crs": {"type": "name", "properties": {"name": PUBLIC_GEOJSON_CRS}}, "features": features}
