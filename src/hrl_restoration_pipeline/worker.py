"""Queue-message entry point for the private, pre-promotion validation stage."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import unquote, urlparse

from . import __version__
from .ingestion import load_submission
from .models import Report
from .registry import SnapshotRegistry
from .reporting import write_reports
from .transformation import as_feature_collection, canonicalize, publicize
from .validation import candidate_profile_errors, read_spatial, validate_records


_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True)
class ReadyEvent:
    container: str
    organization: str
    submission_id: str
    marker_path: str


class BlobStore(Protocol):
    def list(self, container: str, prefix: str) -> list[str]: ...
    def download(self, container: str, name: str) -> bytes: ...
    def exists(self, container: str, name: str) -> bool: ...
    def upload_if_absent(self, container: str, name: str, content: bytes) -> bool: ...


class AzureBlobStore:
    """Small adapter that uses a connection string or managed identity at runtime."""
    def __init__(self, connection_string: str | None, account_url: str | None):
        from azure.storage.blob import BlobServiceClient
        if connection_string:
            self.client = BlobServiceClient.from_connection_string(connection_string)
        elif account_url:
            from azure.identity import DefaultAzureCredential
            self.client = BlobServiceClient(account_url, credential=DefaultAzureCredential())
        else:
            raise ValueError("set HRL_STORAGE_CONNECTION_STRING or HRL_STORAGE_ACCOUNT_URL")

    def list(self, container: str, prefix: str) -> list[str]:
        return [blob.name for blob in self.client.get_container_client(container).list_blobs(name_starts_with=prefix)]

    def download(self, container: str, name: str) -> bytes:
        return self.client.get_blob_client(container, name).download_blob().readall()

    def exists(self, container: str, name: str) -> bool:
        return self.client.get_blob_client(container, name).exists()

    def upload_if_absent(self, container: str, name: str, content: bytes) -> bool:
        from azure.core.exceptions import ResourceExistsError
        try:
            self.client.get_blob_client(container, name).upload_blob(content, overwrite=False)
            return True
        except ResourceExistsError:
            return False


def parse_ready_event(message: str | bytes, raw_prefix: str = "raw-submissions") -> ReadyEvent:
    """Accept Event Grid's array envelope and only its exact BlobCreated marker."""
    try:
        payload = json.loads(message)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("queue message is not JSON") from exc
    if isinstance(payload, list):
        if len(payload) != 1:
            raise ValueError("queue message must contain exactly one Event Grid event")
        payload = payload[0]
    if not isinstance(payload, dict):
        raise ValueError("queue message event must be an object")
    event_type = payload.get("eventType") or payload.get("type")
    if event_type != "Microsoft.Storage.BlobCreated":
        raise ValueError("event is not a Storage BlobCreated event")
    subject, data = payload.get("subject"), payload.get("data")
    if not isinstance(subject, str) or not isinstance(data, dict) or not isinstance(data.get("url"), str):
        raise ValueError("event lacks a BlobCreated subject or data.url")
    matched = re.fullmatch(r"/blobServices/default/containers/([^/]+)/blobs/(.+)", subject)
    if not matched:
        raise ValueError("event subject is not an Azure Blob Storage subject")
    container, path = matched.group(1), unquote(matched.group(2))
    url_path = unquote(urlparse(data["url"]).path).lstrip("/").split("/", 1)
    if len(url_path) != 2 or url_path[0] != container or url_path[1] != path:
        raise ValueError("event subject and data.url disagree")
    parts = path.split("/")
    prefix_parts = [part for part in raw_prefix.strip("/").split("/") if part]
    expected_length = len(prefix_parts) + 3
    if len(parts) != expected_length or parts[:len(prefix_parts)] != prefix_parts or parts[-1] != "_READY":
        raise ValueError("BlobCreated event is outside the raw-submissions/_READY protocol")
    organization, submission_id = parts[-3:-1]
    if not all(_SEGMENT.fullmatch(part) for part in (organization, submission_id)):
        raise ValueError("organization or submission ID is not a safe storage-path segment")
    return ReadyEvent(container, organization, submission_id, path)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _put_tree(store: BlobStore, container: str, prefix: str, directory: Path, names: list[str]) -> None:
    for name in names:
        store.upload_if_absent(container, f"{prefix}/{name}", (directory / name).read_bytes())


def _write_candidate_manifest(directory: Path, report: Report) -> None:
    names = ["canonical-candidate.geojson", "public-candidate.geojson", "validation-report.json", "validation-report.html", "validation-report.pdf", "status.json"]
    payload = {"submission_id": report.submission_id, "status": report.status,
               "pipeline_version": report.pipeline_version, "schema": report.schema,
               "registry": report.registry,
               "artifacts": {name: _sha256((directory / name).read_bytes()) for name in names}}
    (directory / "candidate-manifest.json").write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


class ValidationWorker:
    def __init__(self, store: BlobStore, *, raw_container: str, reports_container: str, candidates_container: str,
                 raw_prefix: str, reports_prefix: str, candidates_prefix: str,
                 registry_path: Path, registry_manifest_path: Path):
        self.store = store; self.raw_container = raw_container
        self.reports_container = reports_container; self.candidates_container = candidates_container
        self.raw_prefix = raw_prefix.strip("/"); self.reports_prefix = reports_prefix.strip("/")
        self.candidates_prefix = candidates_prefix.strip("/")
        self.registry_path = registry_path; self.registry_manifest_path = registry_manifest_path

    def process(self, message: str | bytes) -> str:
        try:
            event = parse_ready_event(message, self.raw_prefix)
        except ValueError as exc:
            # A malformed delivery has no trustworthy submission ID. Retain a
            # private, idempotent diagnostic under a content-derived key rather
            # than accidentally attributing it to a real submission.
            raw_message = message.encode() if isinstance(message, str) else message
            report_prefix = f"{self.reports_prefix}/invalid-events/{_sha256(raw_message)}"
            if self.store.exists(self.reports_container, f"{report_prefix}/status.json"):
                return "already_processed"
            timestamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            report = Report(None, {}, {}, __version__, timestamp)
            report.add("queue_event", "ERROR", "event_grid_ready_protocol", str(exc))
            with tempfile.TemporaryDirectory(prefix="hrl-validation-") as temporary:
                output = Path(temporary)
                write_reports(report, output)
                _put_tree(self.store, self.reports_container, report_prefix, output, ["validation-report.json", "validation-report.html", "validation-report.pdf"])
                self.store.upload_if_absent(self.reports_container, f"{report_prefix}/status.json", json.dumps(report.json(), sort_keys=True).encode())
            return report.status
        if event.container != self.raw_container:
            raise ValueError("event container does not match the configured raw-submissions container")
        report_prefix = f"{self.reports_prefix}/{event.submission_id}"
        candidate_prefix = f"{self.candidates_prefix}/{event.submission_id}"
        # Completion markers are written last. They make at-least-once delivery
        # harmless without treating a partially written attempt as complete.
        if self.store.exists(self.candidates_container, f"{candidate_prefix}/status.json") or self.store.exists(self.reports_container, f"{report_prefix}/status.json"):
            return "already_processed"
        source_prefix = "/".join(part for part in (self.raw_prefix, event.organization, event.submission_id) if part) + "/"
        names = self.store.list(event.container, source_prefix)
        if event.marker_path not in names:
            raise ValueError("_READY marker no longer exists at the event path")
        with tempfile.TemporaryDirectory(prefix="hrl-validation-") as temporary:
            root = Path(temporary)
            source_files: list[dict[str, str]] = []
            for name in names:
                relative = name.removeprefix(source_prefix)
                if not relative or relative == "_READY":
                    continue
                if "/" in relative:
                    raise ValueError("submission contains nested paths; submission files must be direct children")
                content = self.store.download(event.container, name)
                (root / relative).write_bytes(content)
                source_files.append({"path": name, "sha256": _sha256(content)})
            marker = self.store.download(event.container, event.marker_path)
            source_files.append({"path": event.marker_path, "sha256": _sha256(marker)})
            output = root / "output"
            try:
                manifest, source = load_submission(root)
                if manifest["submission_id"] != event.submission_id:
                    raise ValueError("submission.json submission_id does not match _READY path")
                records, spatial = read_spatial(source)
                normalized, report = validate_records(records, SnapshotRegistry(self.registry_path, self.registry_manifest_path), manifest, spatial)
            except (ValueError, OSError) as exc:
                timestamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
                report = Report(event.submission_id, {}, {}, __version__, timestamp, source_files=source_files)
                report.add("package", "ERROR", "queue_submission_validation", str(exc))
                normalized, manifest = [], {}
            report.source_files = source_files
            write_reports(report, output)
            report_files = ["validation-report.json", "validation-report.html", "validation-report.pdf"]
            _put_tree(self.store, self.reports_container, report_prefix, output, report_files)
            if report.errors:
                self.store.upload_if_absent(self.reports_container, f"{report_prefix}/status.json", json.dumps(report.json(), sort_keys=True).encode())
                return report.status
            canonical = canonicalize(normalized, manifest); public = publicize(canonical)
            for record_id, error in candidate_profile_errors(canonical, "RestorationProjectCanonicalRecord"):
                report.add("candidate_schema", "ERROR", "canonical_profile", error, record_id)
            for record_id, error in candidate_profile_errors(public, "RestorationProjectPublicRecord"):
                report.add("candidate_schema", "ERROR", "public_profile", error, record_id)
            if report.errors:
                write_reports(report, output)
                _put_tree(self.store, self.reports_container, report_prefix, output, report_files)
                self.store.upload_if_absent(self.reports_container, f"{report_prefix}/status.json", json.dumps(report.json(), sort_keys=True).encode())
                return report.status
            (output / "canonical-candidate.geojson").write_text(json.dumps(as_feature_collection(canonical), indent=2, sort_keys=True))
            (output / "public-candidate.geojson").write_text(json.dumps(as_feature_collection(public), indent=2, sort_keys=True))
            (output / "status.json").write_text(json.dumps(report.json(), indent=2, sort_keys=True))
            _write_candidate_manifest(output, report)
            _put_tree(self.store, self.candidates_container, candidate_prefix, output, ["canonical-candidate.geojson", "public-candidate.geojson", "validation-report.json", "validation-report.html", "validation-report.pdf", "candidate-manifest.json"])
            self.store.upload_if_absent(self.candidates_container, f"{candidate_prefix}/status.json", (output / "status.json").read_bytes())
            return report.status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate one Event Grid _READY message; never promotes or publishes.")
    parser.add_argument("--message-file", type=Path)
    parser.add_argument("--raw-container", required=True); parser.add_argument("--reports-container", required=True); parser.add_argument("--candidates-container", required=True)
    parser.add_argument("--raw-prefix", default="raw-submissions"); parser.add_argument("--reports-prefix", default="restoration-projects"); parser.add_argument("--candidates-prefix", default="restoration-projects")
    parser.add_argument("--registry", type=Path, required=True); parser.add_argument("--registry-manifest", type=Path, required=True)
    parser.add_argument("--connection-string", default=os.getenv("HRL_STORAGE_CONNECTION_STRING")); parser.add_argument("--account-url", default=os.getenv("HRL_STORAGE_ACCOUNT_URL"))
    args = parser.parse_args(argv)
    message = args.message_file.read_text(encoding="utf-8") if args.message_file else sys.stdin.read()
    if not message.strip(): parser.error("provide --message-file or a queue message on stdin")
    worker = ValidationWorker(AzureBlobStore(args.connection_string, args.account_url), raw_container=args.raw_container, reports_container=args.reports_container, candidates_container=args.candidates_container, raw_prefix=args.raw_prefix, reports_prefix=args.reports_prefix, candidates_prefix=args.candidates_prefix, registry_path=args.registry, registry_manifest_path=args.registry_manifest)
    try:
        print(worker.process(message))
    except ValueError as exc:
        print(f"validation worker rejected message: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
