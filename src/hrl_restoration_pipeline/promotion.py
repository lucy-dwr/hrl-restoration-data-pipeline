"""Local, explicit promotion of an approved validation candidate."""
from __future__ import annotations

import json
import hashlib
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

from shapely.geometry import shape

from .publication import _pointer_checksum, activate_local_snapshot, merge, publish_local
from .transformation import as_feature_collection, publicize
from .validation import candidate_profile_errors, schema_provenance

APPROVAL_MARKER = "_APPROVE"
CANDIDATE_ARTIFACTS = {
    "canonical-candidate.geojson",
    "public-candidate.geojson",
    "status.json",
    "validation-report.json",
    "validation-report.html",
    "validation-report.pdf",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _candidate_manifest(candidate_directory: Path) -> tuple[dict[str, Any], str]:
    path = candidate_directory / "candidate-manifest.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("candidate-manifest.json is required and must contain JSON") from exc
    required = {
        "submission_id", "status", "pipeline_version", "schema", "registry",
        "submission_data_steward", "ingested_by", "artifacts",
    }
    if (not isinstance(manifest, dict) or not required <= set(manifest)
            or not isinstance(manifest["artifacts"], dict)
            or not isinstance(manifest["submission_data_steward"], dict)
            or not all(isinstance(manifest["submission_data_steward"].get(key), str) and manifest["submission_data_steward"][key].strip() for key in {"name", "email"})
            or not isinstance(manifest["ingested_by"], str) or not manifest["ingested_by"].strip()
            or set(manifest["artifacts"]) != CANDIDATE_ARTIFACTS):
        raise ValueError("candidate-manifest.json is incomplete")
    for name, expected in manifest["artifacts"].items():
        artifact = candidate_directory / name
        if (not isinstance(name, str) or Path(name).name != name
                or not isinstance(expected, str) or not artifact.is_file()
                or _sha256(artifact) != expected):
            raise ValueError(f"candidate artifact checksum does not match: {name}")
    return manifest, _sha256(path)


def _load_records(path: Path, profile: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path.name} is unreadable JSON") from exc
    if payload.get("type") != "FeatureCollection" or payload.get("crs", {}).get("properties", {}).get("name") != "EPSG:3310":
        raise ValueError(f"{path.name} must be an EPSG:3310 GeoJSON FeatureCollection")
    records = []
    for feature in payload.get("features", []):
        if feature.get("type") != "Feature" or not isinstance(feature.get("properties"), dict):
            raise ValueError(f"{path.name} contains an invalid feature")
        geometry = feature.get("geometry")
        try:
            parsed = shape(geometry)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{path.name} contains unparsable geometry") from exc
        if parsed.is_empty or not parsed.is_valid:
            raise ValueError(f"{path.name} contains invalid geometry")
        records.append({**feature["properties"], "geometry": geometry})
    errors = candidate_profile_errors(records, profile)
    if errors:
        raise ValueError(f"{path.name} violates {profile}: {errors[0][1]}")
    return sorted(records, key=lambda record: record["project_id"])


def _approval(candidate_directory: Path) -> dict[str, str]:
    marker = candidate_directory / APPROVAL_MARKER
    try:
        approval = json.loads(marker.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"{APPROVAL_MARKER} marker is required before promotion") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{APPROVAL_MARKER} must contain JSON") from exc
    required = {"submission_id", "publication_version", "approved_by", "approved_at", "candidate_manifest_sha256"}
    if not isinstance(approval, dict) or any(not isinstance(approval.get(key), str) or not approval[key].strip() for key in required):
        raise ValueError(f"{APPROVAL_MARKER} requires nonempty submission_id, publication_version, approved_by, approved_at, and candidate_manifest_sha256")
    version = approval["publication_version"]
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})(?:-r([2-9]\d*))?", version)
    if not match:
        raise ValueError(f"{APPROVAL_MARKER} publication_version must be YYYY-MM-DD or YYYY-MM-DD-rN (N >= 2)")
    try:
        date.fromisoformat(match.group(1))
    except ValueError as exc:
        raise ValueError(f"{APPROVAL_MARKER} publication_version must contain a valid ISO date") from exc
    try:
        datetime.fromisoformat(approval["approved_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{APPROVAL_MARKER} approved_at must be ISO-8601") from exc
    return {key: approval[key] for key in required}


def _already_promoted(audit_directory: Path, manifest_checksum: str) -> bool:
    if not audit_directory.is_dir():
        return False
    for audit_path in audit_directory.glob("*.json"):
        try:
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if audit.get("candidate_manifest_sha256") == manifest_checksum:
            return True
    return False


def promote_local(candidate_directory: Path, master_path: Path, public_root: Path, version: str) -> Path:
    """Merge an explicitly approved candidate and create a public snapshot.

    The raw submission and candidate are read only. Records absent from the
    candidate remain in the canonical master; promotion never allocates or
    changes project IDs.
    """
    approval = _approval(candidate_directory)
    if approval["publication_version"] != version:
        raise ValueError("approval marker publication_version does not match requested promotion version")
    manifest, manifest_checksum = _candidate_manifest(candidate_directory)
    if approval["candidate_manifest_sha256"] != manifest_checksum:
        raise ValueError("approval marker does not match the current candidate manifest")
    audit_path = master_path.parent / "promotion-audits" / f"{version}.json"
    if audit_path.exists():
        raise FileExistsError("immutable promotion audit already exists")
    if _already_promoted(audit_path.parent, manifest_checksum):
        raise ValueError("candidate has already been promoted")
    try:
        status = json.loads((candidate_directory / "status.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("candidate status.json is required and must contain JSON") from exc
    if (status.get("status") != "AWAITING_APPROVAL" or status.get("submission_id") != approval["submission_id"]
            or manifest["status"] != status.get("status") or manifest["submission_id"] != status.get("submission_id")
            or manifest["schema"] != status.get("schema") or manifest["registry"] != status.get("registry")):
        raise ValueError("approval marker does not match an AWAITING_APPROVAL candidate")
    candidate = _load_records(candidate_directory / "canonical-candidate.geojson", "RestorationProjectCanonicalRecord")
    existing = _load_records(master_path, "RestorationProjectCanonicalRecord") if master_path.exists() else []
    merged = merge(existing, candidate)
    errors = candidate_profile_errors(merged, "RestorationProjectCanonicalRecord")
    if errors:
        raise ValueError(f"merged master violates RestorationProjectCanonicalRecord: {errors[0][1]}")
    expected_pointer_checksum = _pointer_checksum(public_root)
    snapshot = publish_local(publicize(merged), public_root, version, {
        "schema_version": schema_provenance()["version"],
        "source_submission_id": approval["submission_id"],
        "approved_at": approval["approved_at"],
        "candidate_manifest_sha256": manifest_checksum,
        "registry": manifest["registry"],
    }, update_pointer=False)
    master_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = master_path.with_name(f".{master_path.name}.tmp")
    temporary.write_text(json.dumps(as_feature_collection(merged), indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(master_path)
    audit_path.parent.mkdir(exist_ok=True)
    audit_path.write_text(json.dumps({
        "snapshot_version": version,
        "submission_id": approval["submission_id"],
        "data_steward": manifest["submission_data_steward"],
        "ingested_by": manifest["ingested_by"],
        "approved_by": approval["approved_by"],
        "approved_at": approval["approved_at"],
        "candidate_manifest_sha256": manifest_checksum,
        "schema": manifest["schema"],
        "registry": manifest["registry"],
    }, indent=2, sort_keys=True), encoding="utf-8")
    activate_local_snapshot(public_root, version, expected_pointer_checksum)
    return snapshot
