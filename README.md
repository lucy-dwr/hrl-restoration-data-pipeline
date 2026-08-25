# hrl-restoration-data-pipeline

Local, deterministic ingestion, validation, candidate generation, and publication simulation for HRL restoration projects. It has no Azure orchestration or production database access.

## Queue-triggered validation worker

`hrl-validation-worker` is the container entrypoint for the first Azure-facing
worker foundation. It receives one Azure Storage Queue body on standard input
(or `--message-file`) containing exactly one Event Grid `BlobCreated` event.
It accepts only a `_READY` marker at
`raw-submissions/<organization>/<submission-id>/_READY`; it never processes a
data-file event. The worker re-lists the directory, verifies `_READY` is still
present, stages source blobs read-only, and uses the existing deterministic
validator.

In production, invoke it with `--queue validation-requests`. It receives one
message with a five-minute visibility timeout, acknowledges it only after the
worker records an idempotent outcome, and leaves unexpected runtime failures
unacknowledged for Storage Queue retry/dead-letter handling. The stdin and
`--message-file` modes remain useful for deterministic local tests.

All locations and credentials are runtime configuration. Use either
`HRL_STORAGE_CONNECTION_STRING` for local development or
`HRL_STORAGE_ACCOUNT_URL` with managed identity in Azure; do not put either in
Git. The registry snapshot and its manifest are mounted/provided as arguments.

```bash
hrl-validation-worker \
  --message-file event-grid-message.json \
  --raw-container private-data \
  --reports-container private-data \
  --candidates-container private-data \
  --registry /run/registry/registry.json \
  --registry-manifest /run/registry/manifest.json
```

The event subject must agree with `data.url`. Source paths and SHA-256 checksums
are included in `validation-report.json`. Failed validation writes a private
`validation-reports/restoration-projects/<submission-id>/` report with
`NEEDS_CORRECTION`. A passing submission writes only a private candidate under
`publication-candidates/restoration-projects/<submission-id>/` with
`AWAITING_APPROVAL`; this worker has no promotion or public-export code path.

Event Grid delivery is at least once. Completion `status.json` is written last,
and all artifact writes are create-only, so retries and duplicate messages reuse
the existing result rather than overwriting it. An interrupted attempt is safe
to retry because no completion marker exists. Archive intake rejects traversal,
encrypted files, duplicate entries, excessive member counts, unsafe compression
ratios, oversized extracted payloads, and incomplete shapefile packages.

`--raw-prefix` defaults to `raw-submissions` when all logical areas share one
container. When `raw-submissions` is itself a dedicated container, pass
`--raw-prefix ''`; then the marker is directly
`<organization>/<submission-id>/_READY` inside that container.

```bash
python -m pip install -e '.[test]'
hrl-pipeline <submission-directory> --registry tests/fixtures/registry.json \
  --registry-manifest tests/fixtures/registry-manifest.json --output /tmp/hrl-report
pytest
```

Validation writes authoritative `validation-report.json` plus prominent HTML and PDF companion reports. A successful validation creates schema-validated `canonical-candidate.geojson` and privacy-filtered `public-candidate.geojson`; it is still `AWAITING_APPROVAL`. Local publication is available through `publication.publish_local` and creates immutable directories plus `current.json`.

## Updating the schema snapshot

The configured schema is `hrl-restoration-schema` **v1.2.0**, pinned to its immutable commit and checksum in `schema-snapshots/`. Never edit an existing snapshot or follow the upstream default branch at runtime.

After an approved schema release, use a review branch and run:

```bash
python scripts/import_schema_snapshot.py vX.Y.Z
```

The importer resolves the release tag to its annotated commit, downloads the released LinkML source, calculates its SHA-256 checksum, and refuses to overwrite an existing snapshot. Then update the configured `SCHEMA_PATH` in `src/hrl_restoration_pipeline/validation.py`, add/adjust contract fixtures, run `pytest`, and submit the snapshot, manifest, code, and tests together for review. Schema import is an explicit release change; it must not occur automatically in production.

## Local Phase 2 release checklist

1. Select and record an immutable, checksummed project-registry export snapshot.
2. Import an approved schema release with `python scripts/import_schema_snapshot.py vX.Y.Z`.
3. Change the configured schema version only in a reviewed change with its snapshot and contract tests.
4. Run the full suite: `pytest`.
5. Run `hrl-pipeline` locally on the submission with the selected registry snapshot.
6. Review all warnings plus the canonical and public candidate GeoJSON files. A successful validation is `AWAITING_APPROVAL`; it is not publication.
7. Simulate approved promotion locally with `hrl-pipeline promote` after human approval.
8. Verify `current.json` references the new immutable snapshot and that its GeoJSON, GeoPackage, CSV, and metadata artifacts (including checksums) are present.

## Local approval and promotion

Validation never publishes data. An authorized reviewer must place a reviewed `_APPROVE` JSON marker in the candidate directory before local promotion:

```json
{
  "submission_id":"example-001",
  "approved_by":"local-reviewer",
  "approved_at":"2026-08-24T20:00:00Z",
  "candidate_manifest_sha256":"<SHA-256 of candidate-manifest.json>"
}
```

The marker must match the candidate's `status.json` and the checksum of its generated `candidate-manifest.json`; that status must be `AWAITING_APPROVAL`. Then run:

```bash
hrl-pipeline promote /path/to/candidate \
  --master /path/to/standardized/canonical-master.geojson \
  --public-root /path/to/public-exports/restoration-projects \
  --version 2026-08-24T200000Z
```

Promotion rechecks every candidate artifact checksum and its schema/registry provenance, validates the candidate and any existing local master against the pinned canonical LinkML profile, upserts only submitted IDs, writes a new immutable public snapshot, updates the local canonical master, and records a private immutable promotion audit. It never allocates, alters, retires, or infers project IDs or record lifecycle from an absent record.

## Container image

The Docker image contains the pinned Python dependencies and the GDAL, GEOS,
and PROJ runtime libraries needed for local spatial processing. It contains no
Azure credentials, SDK configuration, or production database access.

Build and run the full suite in the test target:

```bash
docker build --target test --tag hrl-restoration-data-pipeline:test .
```

Build the runtime image:

```bash
docker build --target runtime --tag hrl-restoration-data-pipeline:local .
```

The runtime entry point is `hrl-validation-worker`; pass the queue body on
standard input (or use `--message-file`) and provide its storage/runtime
arguments. `hrl-pipeline` remains the local deterministic validator, while the
existing local-only `promote` command is not part of this worker or container
entrypoint. For example, with suitable local paths mounted:

```bash
docker run --rm -i hrl-restoration-data-pipeline:local \
  --raw-container private-data --reports-container private-data \
  --candidates-container private-data --registry /data/registry.json \
  --registry-manifest /data/registry-manifest.json < event-grid-message.json
```

## Production image release

The image release boundary is documented in [IMAGE_RELEASE.md](IMAGE_RELEASE.md).
The release workflow tests a protected, annotated `v*` tag, uses a dedicated
GitHub OIDC publisher identity to push to ACR, and emits an immutable digest
handoff. It cannot change Terraform or an Azure job; infrastructure consumes
the digest only through a reviewed change.
