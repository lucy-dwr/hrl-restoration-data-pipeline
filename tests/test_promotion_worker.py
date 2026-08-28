import hashlib
import json
from pathlib import Path

import pytest

from hrl_restoration_pipeline.cli import _write_candidate_manifest
from hrl_restoration_pipeline.promotion_worker import PromotionWorker, parse_approval_event
from hrl_restoration_pipeline.registry import SnapshotRegistry
from hrl_restoration_pipeline.reporting import write_reports
from hrl_restoration_pipeline.transformation import as_feature_collection, canonicalize, publicize
from hrl_restoration_pipeline.validation import validate_records


class MemoryBlobStore:
    def __init__(self):
        self.blobs: dict[tuple[str, str], bytes] = {}
        self.etags: dict[tuple[str, str], int] = {}
        self.reject_pointer_update = False

    def download(self, container, name):
        return self.blobs[(container, name)]

    def exists(self, container, name):
        return (container, name) in self.blobs

    def upload_if_absent(self, container, name, content):
        key = (container, name)
        if key in self.blobs:
            return False
        self.blobs[key] = content
        self.etags[key] = self.etags.get(key, 0) + 1
        return True

    def download_with_etag(self, container, name):
        key = (container, name)
        return (self.blobs[key], str(self.etags[key])) if key in self.blobs else (b"", None)

    def upload_if_match(self, container, name, content, etag):
        key = (container, name)
        if name.endswith("current.json") and self.reject_pointer_update:
            return False
        if etag is None:
            return self.upload_if_absent(container, name, content)
        if key not in self.blobs or str(self.etags[key]) != etag:
            return False
        self.blobs[key] = content
        self.etags[key] += 1
        return True


def _event(path="restoration-projects/s1/_APPROVE"):
    return json.dumps([{
        "eventType": "Microsoft.Storage.BlobCreated",
        "subject": f"/blobServices/default/containers/publication-candidates/blobs/{path}",
        "data": {"url": f"https://example.blob.core.windows.net/publication-candidates/{path}"},
    }])


def _candidate(tmp_path):
    fixtures = Path(__file__).parent / "fixtures"
    registry = SnapshotRegistry(fixtures / "registry.json", fixtures / "registry-manifest.json")
    manifest = {
        "submission_id": "s1", "organization_code": "DWR", "data_as_of": "2026-08-20",
        "data_steward_name": "Example Steward", "data_steward_email": "steward@example.org",
    }
    record = {
        "project_id": "HRL-001", "project_name": "Promotion worker fixture",
        "project_description": "Valid fixture", "project_stage": "design",
        "lead_entity": "dwr", "early_implementation": True, "system": "Delta",
        "project_type": "tidal habitat", "acreage": 1.5, "target_species": "Chinook salmon",
    }
    geometry = {"type": "Point", "coordinates": [1, 2]}
    record["geometry"] = geometry
    normalized, report = validate_records([record], registry, manifest, [{"project_id": "HRL-001", "crs": "EPSG:3310", "geometry": geometry}])
    assert report.status == "AWAITING_APPROVAL"
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "canonical-candidate.geojson").write_text(json.dumps(as_feature_collection(canonicalize(normalized, manifest)), sort_keys=True))
    (candidate / "public-candidate.geojson").write_text(json.dumps(as_feature_collection(publicize(canonicalize(normalized, manifest))), sort_keys=True))
    (candidate / "status.json").write_text(json.dumps(report.json(), sort_keys=True))
    write_reports(report, candidate)
    _write_candidate_manifest(candidate, report, manifest)
    approval = {
        "submission_id": "s1", "publication_version": "2026-08-27",
        "approved_by": "reviewer@example.org", "approved_at": "2026-08-27T20:00:00Z",
        "candidate_manifest_sha256": hashlib.sha256((candidate / "candidate-manifest.json").read_bytes()).hexdigest(),
    }
    (candidate / "_APPROVE").write_text(json.dumps(approval, sort_keys=True))
    return candidate


def _store_candidate(store, candidate):
    for path in candidate.iterdir():
        key = ("publication-candidates", f"restoration-projects/s1/{path.name}")
        store.blobs[key] = path.read_bytes()
        store.etags[key] = 1


def _worker(store):
    return PromotionWorker(store, candidates_container="publication-candidates", standardized_container="standardized", public_container="public-exports")


def test_parses_only_an_exact_candidate_approval_marker():
    assert parse_approval_event(_event()).submission_id == "s1"
    with pytest.raises(ValueError):
        parse_approval_event(_event("restoration-projects/s1/public-candidate.geojson"))


def test_promotes_verified_candidate_idempotently(tmp_path):
    store = MemoryBlobStore()
    _store_candidate(store, _candidate(tmp_path))
    worker = _worker(store)

    assert worker.process(_event()) == "PUBLISHED"
    pointer = json.loads(store.blobs[("public-exports", "restoration-projects/current.json")])
    assert pointer["snapshot_version"] == "2026-08-27"
    assert ("standardized", "restoration-projects/canonical-master.geojson") in store.blobs
    audit = json.loads(store.blobs[("standardized", "restoration-projects/promotion-audits/2026-08-27.json")])
    assert audit["submission_id"] == "s1"
    assert worker.process(_event()) == "already_processed"


def test_pointer_conflict_preserves_known_good_current_pointer(tmp_path):
    store = MemoryBlobStore()
    _store_candidate(store, _candidate(tmp_path))
    pointer_name = ("public-exports", "restoration-projects/current.json")
    store.blobs[pointer_name] = json.dumps({"snapshot_version": "2026-08-26"}).encode()
    store.etags[pointer_name] = 1
    store.reject_pointer_update = True

    with pytest.raises(ValueError, match="current.json changed"):
        _worker(store).process(_event())
    assert json.loads(store.blobs[pointer_name])["snapshot_version"] == "2026-08-26"
