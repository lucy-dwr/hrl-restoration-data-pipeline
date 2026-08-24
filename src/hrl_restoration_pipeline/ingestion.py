from __future__ import annotations

import json, zipfile
from datetime import date
from pathlib import Path

REQUIRED_MANIFEST = {"submission_id", "organization", "organization_code", "dataset_name", "submission_type", "submission_scope", "data_as_of", "primary_file"}


def load_submission(directory: Path) -> tuple[dict, Path]:
    manifest_path = directory / "submission.json"
    if not manifest_path.is_file():
        raise ValueError("submission.json is required")
    try: manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as exc: raise ValueError("submission.json is unreadable JSON") from exc
    missing = REQUIRED_MANIFEST - set(manifest)
    if missing: raise ValueError(f"submission manifest missing: {', '.join(sorted(missing))}")
    if any(not isinstance(manifest[key], str) or not manifest[key].strip() for key in REQUIRED_MANIFEST):
        raise ValueError("submission manifest required fields must be nonempty strings")
    try: date.fromisoformat(manifest["data_as_of"])
    except ValueError as exc: raise ValueError("data_as_of must be an ISO-8601 date") from exc
    if manifest["submission_type"] not in {"update", "correction"}: raise ValueError("invalid submission_type")
    if manifest["submission_scope"] not in {"complete_organization_snapshot", "partial_update"}: raise ValueError("invalid submission_scope")
    if manifest["submission_type"] == "correction" and not manifest.get("supersedes_submission_id"): raise ValueError("correction requires supersedes_submission_id")
    primary = directory / manifest["primary_file"]
    candidates = [p for p in directory.iterdir() if p.suffix.lower() in {".gpkg", ".geojson", ".zip"}]
    if primary not in candidates or len(candidates) != 1: raise ValueError("exactly one declared primary spatial input is required")
    if primary.suffix.lower() == ".zip":
        try:
            with zipfile.ZipFile(primary) as archive:
                names = [Path(n).name for n in archive.namelist() if not n.endswith("/")]
                stems = {str(Path(n).with_suffix("")) for n in names if Path(n).suffix.lower() == ".shp"}
                if len(stems) != 1: raise ValueError("archive must contain exactly one shapefile")
                stem = next(iter(stems)); required = {f"{stem}{x}" for x in (".shp", ".shx", ".dbf", ".prj")}
                if not required <= set(names): raise ValueError("incomplete shapefile package")
        except zipfile.BadZipFile as exc: raise ValueError("corrupt shapefile archive") from exc
    return manifest, primary
