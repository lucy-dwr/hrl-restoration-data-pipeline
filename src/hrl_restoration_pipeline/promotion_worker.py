"""Queue-message entry point for approval-gated restoration publication."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import unquote, urlparse

from .promotion import CANDIDATE_ARTIFACTS, promote_local
from .worker import AzureBlobStore, AzureQueue


_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True)
class ApprovalEvent:
    container: str
    submission_id: str
    marker_path: str


class BlobStore(Protocol):
    def download(self, container: str, name: str) -> bytes: ...
    def exists(self, container: str, name: str) -> bool: ...
    def upload_if_absent(self, container: str, name: str, content: bytes) -> bool: ...
    def download_with_etag(self, container: str, name: str) -> tuple[bytes, str | None]: ...
    def upload_if_match(self, container: str, name: str, content: bytes, etag: str | None) -> bool: ...


def parse_approval_event(message: str | bytes, candidate_prefix: str = "restoration-projects") -> ApprovalEvent:
    """Accept one Event Grid BlobCreated event for an exact candidate _APPROVE."""
    try:
        payload = json.loads(message)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("queue message is not JSON") from exc
    if isinstance(payload, list):
        if len(payload) != 1:
            raise ValueError("queue message must contain exactly one Event Grid event")
        payload = payload[0]
    if not isinstance(payload, dict) or payload.get("eventType") != "Microsoft.Storage.BlobCreated":
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
    prefix = [part for part in candidate_prefix.strip("/").split("/") if part]
    if len(parts) != len(prefix) + 2 or parts[:len(prefix)] != prefix or parts[-1] != "_APPROVE":
        raise ValueError("BlobCreated event is outside the publication-candidates/_APPROVE protocol")
    submission_id = parts[-2]
    if not _SEGMENT.fullmatch(submission_id):
        raise ValueError("submission ID is not a safe storage-path segment")
    return ApprovalEvent(container, submission_id, path)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _json(content: bytes, name: str) -> dict:
    try:
        value = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{name} must contain JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{name} must contain a JSON object")
    return value


class PromotionWorker:
    """Promote one verified candidate, leaving unexpected failures for retry."""

    def __init__(self, store: BlobStore, *, candidates_container: str, standardized_container: str,
                 public_container: str, candidates_prefix: str = "restoration-projects",
                 standardized_prefix: str = "restoration-projects",
                 public_prefix: str = "restoration-projects"):
        self.store = store
        self.candidates_container = candidates_container
        self.standardized_container = standardized_container
        self.public_container = public_container
        self.candidates_prefix = candidates_prefix.strip("/")
        self.standardized_prefix = standardized_prefix.strip("/")
        self.public_prefix = public_prefix.strip("/")

    def _candidate_prefix(self, submission_id: str) -> str:
        return f"{self.candidates_prefix}/{submission_id}"

    def _audit_name(self, version: str) -> str:
        return f"{self.standardized_prefix}/promotion-audits/{version}.json"

    def _write_immutable(self, container: str, name: str, content: bytes) -> None:
        """Create an immutable blob, accepting only an identical retry."""
        if self.store.upload_if_absent(container, name, content):
            return
        if not self.store.exists(container, name) or self.store.download(container, name) != content:
            raise FileExistsError(f"immutable artifact already exists with different content: {name}")

    def process(self, message: str | bytes) -> str:
        event = parse_approval_event(message, self.candidates_prefix)
        if event.container != self.candidates_container:
            raise ValueError("event container does not match the configured publication-candidates container")
        prefix = self._candidate_prefix(event.submission_id)
        if not self.store.exists(event.container, event.marker_path):
            raise ValueError("_APPROVE marker no longer exists at the event path")

        required = set(CANDIDATE_ARTIFACTS) | {"candidate-manifest.json", "_APPROVE"}
        with tempfile.TemporaryDirectory(prefix="hrl-promotion-") as temporary:
            root = Path(temporary)
            candidate = root / "candidate"
            candidate.mkdir()
            for name in required:
                blob_name = f"{prefix}/{name}"
                if not self.store.exists(event.container, blob_name):
                    raise ValueError(f"candidate artifact is missing: {name}")
                (candidate / name).write_bytes(self.store.download(event.container, blob_name))

            approval = _json((candidate / "_APPROVE").read_bytes(), "_APPROVE")
            version = approval.get("publication_version")
            if not isinstance(version, str):
                raise ValueError("_APPROVE publication_version is required")
            audit_name = self._audit_name(version)
            candidate_manifest_checksum = _sha256((candidate / "candidate-manifest.json").read_bytes())
            if self.store.exists(self.standardized_container, audit_name):
                audit = _json(self.store.download(self.standardized_container, audit_name), "promotion audit")
                if audit.get("candidate_manifest_sha256") != candidate_manifest_checksum:
                    raise ValueError("publication version is already associated with a different candidate")
                pointer_content, _ = self.store.download_with_etag(self.public_container, f"{self.public_prefix}/current.json")
                if pointer_content and _json(pointer_content, "current.json").get("snapshot_version") == version:
                    return "already_processed"
            master_name = f"{self.standardized_prefix}/canonical-master.geojson"
            master_content, master_etag = self.store.download_with_etag(self.standardized_container, master_name)
            master = root / "canonical-master.geojson"
            if master_etag is not None:
                master.write_bytes(master_content)
            public_root = root / "public-exports"
            pointer_name = f"{self.public_prefix}/current.json"
            pointer_content, pointer_etag = self.store.download_with_etag(self.public_container, pointer_name)
            if pointer_etag is not None:
                public_root.mkdir(parents=True)
                (public_root / "current.json").write_bytes(pointer_content)

            # promote_local verifies the approval marker, candidate status,
            # checksums, schema/registry provenance, and immutable version.
            snapshot = promote_local(candidate, master, public_root, version)
            for artifact in sorted(path for path in snapshot.iterdir() if path.is_file()):
                name = f"{self.public_prefix}/{version}/{artifact.name}"
                self._write_immutable(self.public_container, name, artifact.read_bytes())

            audit_path = master.parent / "promotion-audits" / f"{version}.json"
            audit_content = audit_path.read_bytes()
            if self.store.exists(self.standardized_container, audit_name):
                audit = _json(self.store.download(self.standardized_container, audit_name), "promotion audit")
                if audit.get("candidate_manifest_sha256") != candidate_manifest_checksum:
                    raise ValueError("publication version is already associated with a different candidate")
            self._write_immutable(self.standardized_container, audit_name, audit_content)
            if not self.store.upload_if_match(self.standardized_container, master_name, master.read_bytes(), master_etag):
                raise ValueError("canonical master changed during promotion")
            if not self.store.upload_if_match(self.public_container, pointer_name, (public_root / "current.json").read_bytes(), pointer_etag):
                raise ValueError("current.json changed during promotion")
            return "PUBLISHED"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Promote one approved Event Grid candidate; preserves immutable snapshots and current.json on conflicts.")
    input_mode = parser.add_mutually_exclusive_group()
    input_mode.add_argument("--message-file", type=Path)
    input_mode.add_argument("--queue", help="receive and acknowledge one Storage Queue message")
    parser.add_argument("--candidates-container", required=True)
    parser.add_argument("--standardized-container", required=True)
    parser.add_argument("--public-container", required=True)
    parser.add_argument("--candidates-prefix", default="restoration-projects")
    parser.add_argument("--standardized-prefix", default="restoration-projects")
    parser.add_argument("--public-prefix", default="restoration-projects")
    parser.add_argument("--connection-string", default=os.getenv("HRL_STORAGE_CONNECTION_STRING"))
    parser.add_argument("--account-url", default=os.getenv("HRL_STORAGE_ACCOUNT_URL"))
    args = parser.parse_args(argv)
    queue = AzureQueue(args.queue, args.connection_string, args.account_url) if args.queue else None
    queued_message = queue.receive_one() if queue else None
    message = args.message_file.read_text(encoding="utf-8") if args.message_file else queued_message.content if queued_message else sys.stdin.read()
    if not message.strip():
        if queue:
            return 0
        parser.error("provide --message-file, --queue, or a queue message on stdin")
    worker = PromotionWorker(
        AzureBlobStore(args.connection_string, args.account_url),
        candidates_container=args.candidates_container,
        standardized_container=args.standardized_container,
        public_container=args.public_container,
        candidates_prefix=args.candidates_prefix,
        standardized_prefix=args.standardized_prefix,
        public_prefix=args.public_prefix,
    )
    try:
        print(worker.process(message))
        if queue:
            queue.acknowledge(queued_message)
    except (ValueError, FileExistsError) as exc:
        print(f"promotion worker rejected message: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
