from __future__ import annotations
import argparse, hashlib, json
from pathlib import Path
from .ingestion import load_submission
from .registry import SnapshotRegistry
from .reporting import write_reports
from .transformation import as_feature_collection, canonicalize, publicize
from .validation import candidate_profile_errors, read_spatial, validate_records


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_candidate_manifest(directory: Path, report) -> None:
    artifacts = {name: _sha256(directory / name) for name in (
        "canonical-candidate.geojson", "public-candidate.geojson", "status.json",
        "validation-report.json", "validation-report.html", "validation-report.pdf",
    )}
    payload = {
        "submission_id": report.submission_id,
        "status": report.status,
        "pipeline_version": report.pipeline_version,
        "schema": report.schema,
        "registry": report.registry,
        "artifacts": artifacts,
    }
    directory.joinpath("candidate-manifest.json").write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

def _validate_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hrl-pipeline")
    parser.add_argument("submission", type=Path); parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--registry-manifest", type=Path, required=True); parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        manifest, source = load_submission(args.submission)
        records, spatial = read_spatial(source)
        normalized, report = validate_records(records, SnapshotRegistry(args.registry, args.registry_manifest), manifest, spatial)
    except (ValueError, OSError) as exc:
        from .models import Report
        report = Report(None, {}, {}, "0.2.0", "unavailable"); report.add("package", "ERROR", "package_validation", str(exc)); normalized = []; manifest = {}
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
    write_reports(report, args.output)
    if candidate_ready:
        _write_candidate_manifest(args.output, report)
    return 0 if not report.errors else 2


def _promote_main(argv: list[str]) -> int:
    from .promotion import promote_local

    parser = argparse.ArgumentParser(prog="hrl-pipeline promote")
    parser.add_argument("candidate", type=Path, help="AWAITING_APPROVAL candidate directory containing _APPROVE")
    parser.add_argument("--master", type=Path, required=True, help="local canonical-master.geojson path")
    parser.add_argument("--public-root", type=Path, required=True, help="local immutable public snapshot root")
    parser.add_argument("--version", required=True, help="new immutable public snapshot version")
    args = parser.parse_args(argv)
    try:
        promote_local(args.candidate, args.master, args.public_root, args.version)
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
