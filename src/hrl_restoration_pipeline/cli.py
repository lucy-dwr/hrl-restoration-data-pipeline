from __future__ import annotations
import argparse, hashlib, json, os
from pathlib import Path
from . import __version__
from .ingestion import load_submission
from .registry import CsvRegistry
from .reporting import write_reports
from .transformation import as_feature_collection, canonicalize, publicize
from .validation import candidate_profile_errors, read_spatial, validate_records

CANDIDATE_REPORT_ARTIFACTS = ("validation-report.json", "validation-report.html")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _input_file_metadata(source: Path, records: list[dict], spatial: list[dict]) -> dict[str, str]:
    metadata = {
        "filename": source.name,
        "format": source.suffix.lower().removeprefix("."),
        "size_bytes": str(source.stat().st_size),
        "sha256": _sha256(source),
        "feature_count": str(len(records)),
        "source_crs": str(next((item.get("source_crs") for item in spatial if item.get("source_crs")), "unavailable")),
        "validated_crs": str(next((item.get("crs") for item in spatial if item.get("crs")), "unavailable")),
    }
    if source.suffix.lower() == ".gpkg":
        import pyogrio
        metadata["layer"] = str(pyogrio.list_layers(source)[0][0])
    return metadata


def _write_candidate_manifest(directory: Path, report, submission_manifest: dict) -> None:
    names = ("canonical-candidate.geojson", "public-candidate.geojson", "status.json", *CANDIDATE_REPORT_ARTIFACTS)
    artifacts = {name: _sha256(directory / name) for name in names}
    payload = {
        "submission_id": report.submission_id,
        "status": report.status,
        "pipeline_version": report.pipeline_version,
        "schema": report.schema,
        "registry": report.registry,
        "submission_data_steward": {
            "name": submission_manifest["data_steward_name"],
            "email": submission_manifest["data_steward_email"],
        },
        "ingested_by": os.environ.get("HRL_INGESTION_ACTOR", "hrl-restoration-pipeline"),
        "artifacts": artifacts,
    }
    directory.joinpath("candidate-manifest.json").write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

def _validate_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hrl-pipeline")
    parser.add_argument("submission", type=Path)
    parser.add_argument("--registry", type=Path, required=True, help="project-id-registry.csv checked out at a known commit")
    parser.add_argument("--registry-ref", default=None, help="commit or tag the registry CSV is pinned to, recorded in the report")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pdf", action="store_true", help="also write a PDF companion report (needs the 'pdf' extra)")
    args = parser.parse_args(argv)
    try:
        manifest, source = load_submission(args.submission)
        records, spatial = read_spatial(source)
        normalized, report = validate_records(records, CsvRegistry(args.registry, args.registry_ref), manifest, spatial)
        report.submission_metadata = {key: str(value) for key, value in manifest.items()}
        report.input_file = _input_file_metadata(source, records, spatial)
    except (ValueError, OSError) as exc:
        from .models import Report
        report = Report(None, {}, {}, __version__, "unavailable"); report.add("package", "ERROR", "package_validation", str(exc)); normalized = []; manifest = {}
    candidate_ready = False
    if not report.errors:
        canonical = canonicalize(normalized, manifest)
        public = publicize(canonical)
        for record_id, message in candidate_profile_errors(canonical, "RestorationProjectCanonicalRecord"):
            report.add("candidate_schema", "ERROR", "canonical_profile", message, record_id)
        for record_id, message in candidate_profile_errors(public, "RestorationProjectPublicRecord"):
            report.add("candidate_schema", "ERROR", "public_profile", message, record_id)
        if not report.errors:
            args.output.mkdir(parents=True, exist_ok=True)
            args.output.joinpath("canonical-candidate.geojson").write_text(json.dumps(as_feature_collection(canonical), indent=2, sort_keys=True))
            args.output.joinpath("public-candidate.geojson").write_text(json.dumps(as_feature_collection(public), indent=2, sort_keys=True))
            args.output.joinpath("status.json").write_text(json.dumps({"submission_id": manifest["submission_id"], "status": report.status, "warning_count": len(report.warnings), "pipeline_version": report.pipeline_version, "schema": report.schema, "registry": report.registry}, indent=2, sort_keys=True))
            candidate_ready = True
    write_reports(report, args.output, pdf=args.pdf)
    if candidate_ready:
        _write_candidate_manifest(args.output, report, manifest)
    return 0 if not report.errors else 2


def _promote_main(argv: list[str]) -> int:
    from .promotion import promote_local

    parser = argparse.ArgumentParser(prog="hrl-pipeline promote")
    parser.add_argument("candidate", type=Path, help="AWAITING_APPROVAL candidate directory containing _APPROVE")
    parser.add_argument("--canonical", type=Path, required=True, help="local canonical-restoration-projects.geojson path")
    parser.add_argument("--public-root", type=Path, required=True, help="local immutable public snapshot root")
    parser.add_argument("--version", required=True, help="must match _APPROVE publication_version")
    args = parser.parse_args(argv)
    try:
        promote_local(args.candidate, args.canonical, args.public_root, args.version)
    except (ValueError, OSError, FileExistsError) as exc:
        parser.error(str(exc))
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = list(argv) if argv is not None else None
    if arguments is None:
        import sys
        arguments = sys.argv[1:]
    if arguments and arguments[0] == "promote":
        return _promote_main(arguments[1:])
    return _validate_main(arguments)

if __name__ == "__main__": raise SystemExit(main())
