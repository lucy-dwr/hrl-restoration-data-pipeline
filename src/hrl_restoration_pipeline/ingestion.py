from __future__ import annotations

import json, re, zipfile
from datetime import date
from pathlib import Path

REQUIRED_MANIFEST = {
    "submission_id", "organization", "organization_code", "dataset_name",
    "submission_type", "submission_scope", "data_as_of", "primary_file",
    "data_steward_name", "data_steward_email",
}
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_ARCHIVE_MEMBERS = 1_000
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 1_000_000_000
MAX_ARCHIVE_COMPRESSION_RATIO = 100


def _validate_shapefile_archive(primary: Path) -> None:
    """Reject unsafe archives before GDAL gets an opportunity to extract them."""
    try:
        with zipfile.ZipFile(primary) as archive:
            entries = [entry for entry in archive.infolist() if not entry.is_dir()]
            if not entries:
                raise ValueError("archive is empty")
            if len(entries) > MAX_ARCHIVE_MEMBERS:
                raise ValueError("archive contains too many files")
            total = sum(entry.file_size for entry in entries)
            if total > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                raise ValueError("archive is too large when extracted")
            names: list[str] = []
            for entry in entries:
                path = Path(entry.filename)
                if path.is_absolute() or ".." in path.parts or entry.flag_bits & 0x1:
                    raise ValueError("archive contains an unsafe path or encrypted file")
                if entry.compress_size and entry.file_size / entry.compress_size > MAX_ARCHIVE_COMPRESSION_RATIO:
                    raise ValueError("archive compression ratio is unsafe")
                names.append(entry.filename)
            if len(set(names)) != len(names):
                raise ValueError("archive contains duplicate paths")
            # Shapefile components can live under one harmless directory, but
            # must form exactly one package with a single basename.
            stems = {str(Path(n).with_suffix("")) for n in names if Path(n).suffix.lower() == ".shp"}
            if len(stems) != 1:
                raise ValueError("archive must contain exactly one shapefile")
            stem = next(iter(stems)); required = {f"{stem}{x}" for x in (".shp", ".shx", ".dbf", ".prj")}
            if not required <= set(names):
                raise ValueError("incomplete shapefile package")
    except zipfile.BadZipFile as exc:
        raise ValueError("corrupt shapefile archive") from exc


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
    if not EMAIL_PATTERN.fullmatch(manifest["data_steward_email"].strip()):
        raise ValueError("data_steward_email must be a valid email address")
    try: date.fromisoformat(manifest["data_as_of"])
    except ValueError as exc: raise ValueError("data_as_of must be an ISO-8601 date") from exc
    if manifest["submission_type"] not in {"update", "correction"}: raise ValueError("invalid submission_type")
    if manifest["submission_scope"] not in {"complete_organization_snapshot", "partial_update"}: raise ValueError("invalid submission_scope")
    if manifest["submission_type"] == "correction" and not manifest.get("supersedes_submission_id"): raise ValueError("correction requires supersedes_submission_id")
    declared = Path(manifest["primary_file"])
    if declared.name != manifest["primary_file"] or manifest["primary_file"] in {".", ".."}:
        raise ValueError("primary_file must be a filename in the submission directory")
    primary = directory / manifest["primary_file"]
    candidates = [p for p in directory.iterdir() if p.suffix.lower() in {".gpkg", ".geojson", ".zip"}]
    if primary not in candidates or len(candidates) != 1: raise ValueError("exactly one declared primary spatial input is required")
    if primary.suffix.lower() == ".zip":
        _validate_shapefile_archive(primary)
    return manifest, primary
