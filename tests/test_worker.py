import json
import zipfile
from pathlib import Path

import pytest

from hrl_restoration_pipeline.ingestion import load_submission
from hrl_restoration_pipeline.worker import ValidationWorker, parse_ready_event, stage_registry_export


class MemoryBlobStore:
    def __init__(self):
        self.blobs: dict[tuple[str, str], bytes] = {}
        self.uploads = 0

    def list(self, container, prefix):
        return sorted(name for current, name in self.blobs if current == container and name.startswith(prefix))

    def download(self, container, name):
        return self.blobs[(container, name)]

    def exists(self, container, name):
        return (container, name) in self.blobs

    def upload_if_absent(self, container, name, content):
        if self.exists(container, name):
            return False
        self.blobs[(container, name)] = content
        self.uploads += 1
        return True


def event(path="raw-submissions/dwr/s1/_READY"):
    return json.dumps([{
        "eventType": "Microsoft.Storage.BlobCreated",
        "subject": f"/blobServices/default/containers/private/blobs/{path}",
        "data": {"url": f"https://example.blob.core.windows.net/private/{path}"},
    }])


def test_parses_only_a_valid_ready_marker():
    parsed = parse_ready_event(event())
    assert (parsed.container, parsed.organization, parsed.submission_id) == ("private", "dwr", "s1")


def test_parses_a_ready_marker_at_the_root_of_a_dedicated_container():
    payload = event("dwr/s1/_READY")
    assert parse_ready_event(payload, raw_prefix="").submission_id == "s1"


@pytest.mark.parametrize("path", [
    "raw-submissions/dwr/s1/projects.geojson",
    "raw-submissions/dwr/s1/nested/_READY",
    "raw-submissions/../s1/_READY",
])
def test_rejects_malformed_or_non_marker_paths(path):
    with pytest.raises(ValueError):
        parse_ready_event(event(path))


def test_rejects_subject_url_disagreement():
    payload = json.loads(event())[0]
    payload["data"]["url"] = "https://example.blob.core.windows.net/private/raw-submissions/dwr/other/_READY"
    with pytest.raises(ValueError, match="disagree"):
        parse_ready_event(json.dumps([payload]))


def _worker(store, tmp_path):
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"records": []}))
    registry_manifest = tmp_path / "registry-manifest.json"
    registry_manifest.write_text(json.dumps({"source_registry": "test", "export_version": "v1", "checksums": {"registry.json": __import__("hashlib").sha256(registry.read_bytes()).hexdigest()}}))
    return ValidationWorker(store, raw_container="private", reports_container="private", candidates_container="private", raw_prefix="raw-submissions", reports_prefix="validation-reports/restoration-projects", candidates_prefix="publication-candidates/restoration-projects", registry_path=registry, registry_manifest_path=registry_manifest)


def test_stages_only_one_named_immutable_registry_export(tmp_path):
    store = MemoryBlobStore()
    export = b'[{"project_id":"HRL-001","status":"eligible"}]'
    manifest = json.dumps({"source_registry": "test", "export_version": "2026-08-24", "checksums": {"project-id-registry.json": __import__("hashlib").sha256(export).hexdigest()}}).encode()
    prefix = "project-id-registry/2026-08-24/"
    store.blobs[("registry-exports", prefix + "manifest.json")] = manifest
    store.blobs[("registry-exports", prefix + "project-id-registry.json")] = export
    registry_path, manifest_path = stage_registry_export(store, "registry-exports", prefix, tmp_path / "registry")
    assert registry_path.read_bytes() == export
    assert json.loads(manifest_path.read_text())["export_version"] == "2026-08-24"
    with pytest.raises(ValueError, match="registry prefix"):
        stage_registry_export(store, "registry-exports", "project-id-registry/current.json", tmp_path / "bad")


def test_missing_manifest_writes_private_correction_report_and_is_idempotent(tmp_path):
    store = MemoryBlobStore()
    store.blobs[("private", "raw-submissions/dwr/s1/_READY")] = b""
    worker = _worker(store, tmp_path)
    assert worker.process(event()) == "NEEDS_CORRECTION"
    report_name = "validation-reports/restoration-projects/s1/validation-report.json"
    report = json.loads(store.blobs[("private", report_name)])
    assert report["status"] == "NEEDS_CORRECTION"
    assert report["submission_id"] == "s1"
    assert report["source_files"][0]["path"].endswith("/_READY")
    uploads = store.uploads
    assert worker.process(event()) == "already_processed"
    assert store.uploads == uploads


def test_malformed_delivery_becomes_an_unattributed_private_correction_report(tmp_path):
    store = MemoryBlobStore()
    assert _worker(store, tmp_path).process("not-json") == "NEEDS_CORRECTION"
    report_names = [name for container, name in store.blobs if container == "private" and name.endswith("validation-report.json")]
    assert len(report_names) == 1
    report = json.loads(store.blobs[("private", report_names[0])])
    assert report["submission_id"] is None and report["status"] == "NEEDS_CORRECTION"


def test_missing_primary_and_unsupported_primary_are_correction_reports(tmp_path):
    store = MemoryBlobStore()
    prefix = "raw-submissions/dwr/s1/"
    manifest = {"submission_id": "s1", "organization": "DWR", "organization_code": "DWR", "dataset_name": "test", "submission_type": "update", "submission_scope": "partial_update", "data_as_of": "2026-08-20", "data_steward_name": "Example Steward", "data_steward_email": "steward@example.org", "primary_file": "projects.csv"}
    store.blobs[("private", prefix + "submission.json")] = json.dumps(manifest).encode()
    store.blobs[("private", prefix + "projects.csv")] = b"not supported"
    store.blobs[("private", prefix + "_READY")] = b""
    assert _worker(store, tmp_path).process(event()) == "NEEDS_CORRECTION"
    payload = json.loads(store.blobs[("private", "validation-reports/restoration-projects/s1/validation-report.json")])
    assert any("exactly one" in finding["message"] for finding in payload["findings"])


def test_archive_safety_rejects_traversal_and_oversized_ratio(tmp_path):
    manifest = {"submission_id": "s1", "organization": "DWR", "organization_code": "DWR", "dataset_name": "test", "submission_type": "update", "submission_scope": "partial_update", "data_as_of": "2026-08-20", "data_steward_name": "Example Steward", "data_steward_email": "steward@example.org", "primary_file": "projects.zip"}
    (tmp_path / "submission.json").write_text(json.dumps(manifest))
    with zipfile.ZipFile(tmp_path / "projects.zip", "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("../projects.shp", b"x")
        archive.writestr("../projects.shx", b"x")
        archive.writestr("../projects.dbf", b"x")
        archive.writestr("../projects.prj", b"x")
    with pytest.raises(ValueError, match="unsafe"):
        load_submission(tmp_path)
    with zipfile.ZipFile(tmp_path / "projects.zip", "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("projects.shp", b"x" * 100_000)
        archive.writestr("projects.shx", b"x")
        archive.writestr("projects.dbf", b"x")
        archive.writestr("projects.prj", b"x")
    with pytest.raises(ValueError, match="compression ratio"):
        load_submission(tmp_path)
