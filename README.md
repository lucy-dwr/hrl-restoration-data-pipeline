# hrl-restoration-data-pipeline

Deterministic, local validation and promotion for HRL restoration project
submissions. No Azure orchestration, no queue workers, no database.

The HRL data operator runs these two commands as part of the email-to-
publication workflow. Cross-repository context is in
[`hrl-azure-infrastructure/PIPELINE_INFRA.md`](https://github.com/lucy-dwr/hrl-azure-infrastructure/blob/main/PIPELINE_INFRA.md);
the step-by-step operator routine is
[`hrl-azure-infrastructure/docs/operator-checklist.md`](https://github.com/lucy-dwr/hrl-azure-infrastructure/blob/main/docs/operator-checklist.md).

## Install

Python 3.11+ and a GDAL/GEOS/PROJ stack (via `geopandas`/`pyogrio` wheels, or
the optional Docker image).

```bash
python -m pip install -e '.[test]'      # add ,pdf for PDF reports
```

## Validate a submission

```bash
hrl-pipeline <submission-directory> \
  --registry /path/to/hrl-project-registry/project-id-registry.csv \
  --registry-ref <commit-or-tag the registry is pinned to> \
  --output ./out/<submission-id>
```

The submission directory contains `submission.json`, one primary spatial file
(`.gpkg`, `.geojson`, or a `.zip` shapefile package), and `_READY`. Validation
checks package safety, geometry, the pinned LinkML profiles, controlled
vocabularies, business rules, and the pinned project-ID registry. It stages the
source read-only and writes create-only artifacts.

Outputs in `--output`:

| Outcome | Files | Exit code |
| --- | --- | --- |
| `AWAITING_APPROVAL` | `validation-report.json`, `validation-report.html`, `canonical-candidate.geojson`, `public-candidate.geojson`, `candidate-manifest.json`, `status.json` | 0 |
| `NEEDS_CORRECTION` | `validation-report.json`, `validation-report.html`, `status.json` | 2 |

Add `--pdf` for a `validation-report.pdf` companion (needs the `pdf` extra). The
JSON report is authoritative; the HTML is the copy an operator forwards to a
provider.

## Promote an approved candidate

An authorized reviewer places a JSON `_APPROVE` marker in the candidate
directory (start from [`templates/approval-template.json`](templates/approval-template.json)):

```json
{
  "submission_id": "2026-08-24_dwr_restoration-projects_v001",
  "publication_version": "2026-08-24",
  "approved_by": "reviewer@example.org",
  "approved_at": "2026-08-24T20:00:00Z",
  "candidate_manifest_sha256": "<sha256 of candidate-manifest.json>"
}
```

Then:

```bash
hrl-pipeline promote ./out/<submission-id> \
  --master /path/to/standardized/canonical-master.geojson \
  --public-root /path/to/public-exports/restoration-map/restoration-projects \
  --version 2026-08-24
```

Promotion re-checks every candidate artifact checksum and the schema/registry
provenance against `_APPROVE`, merges only submitted IDs into the canonical
master, writes a previously unused immutable public snapshot, updates
`current.json` last, and records a private promotion audit. A failed or
interrupted promotion preserves the previous `current.json`.

## Schema snapshot

The pipeline pins `hrl-restoration-schema` **v1.3.1** by immutable commit and
checksum under `schema-snapshots/`. After an approved schema release, on a
review branch:

```bash
python scripts/import_schema_snapshot.py vX.Y.Z
```

Then update `SCHEMA_PATH` in `src/hrl_restoration_pipeline/validation.py`,
adjust fixtures, and run `pytest`. Schema import is an explicit reviewed change.

## Project-ID registry input

Pass `--registry` a checkout of `project-id-registry.csv` from
`hrl-project-registry` at a specific commit, and record that commit with
`--registry-ref`. The pipeline reads `project_id` and `status`; it never
allocates or alters IDs and never follows a moving pointer.

## Tests

```bash
pytest
```

Deterministic and offline: explicit schema snapshot, a synthetic registry
fixture, no Azure. The optional Docker image (`docker build --target test .`)
runs the same suite.

## What was removed

The queue-consuming `hrl-validation-worker` and `hrl-promotion-worker`
entrypoints, the image-release workflow, and the Azure SDK dependencies were
removed when the workflow moved to operator-run commands. See
`PIPELINE_INFRA.md`.
