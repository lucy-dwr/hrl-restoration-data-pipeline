# hrl-restoration-data-pipeline

Deterministic, local validation and promotion for HRL restoration project
submissions. No Azure orchestration, no queue workers, no database.

> **If you are the data operator:** do not set this up from this README. Start
> at
> [`hrl-azure-infrastructure/RESTORATION_DATA_WORKFLOW.md`](https://github.com/Healthy-Rivers-and-Landscapes-Science/hrl-azure-infrastructure/blob/main/RESTORATION_DATA_WORKFLOW.md)
> &mdash; it has the full one-time setup for Windows and macOS, and the routine
> to run each time.

> **If you maintain this tool:** read [`CONTRIBUTING.md`](CONTRIBUTING.md).

Cross-repository context is in
[`hrl-azure-infrastructure/PIPELINE_INFRA.md`](https://github.com/Healthy-Rivers-and-Landscapes-Science/hrl-azure-infrastructure/blob/main/PIPELINE_INFRA.md);
the short operator routine is
[`hrl-azure-infrastructure/docs/operator-checklist.md`](https://github.com/Healthy-Rivers-and-Landscapes-Science/hrl-azure-infrastructure/blob/main/docs/operator-checklist.md);
roles and ownership are in
[`DIVISION_OF_RESPONSIBILITIES.md`](https://github.com/Healthy-Rivers-and-Landscapes-Science/hrl-azure-infrastructure/blob/main/DIVISION_OF_RESPONSIBILITIES.md).

The operator installs from the `main` branch and reinstalls before every run;
there are no release tags. See [`CONTRIBUTING.md` &rarr; "Release
model"](CONTRIBUTING.md#release-model-main-is-the-release-channel).

## Install

Needs Python 3.11+ and a GDAL / GEOS / PROJ stack.

```bash
python -m pip install '.[pdf]'
```

The `.[pdf]` extra adds the PDF report. For development tests, install `.[test]`.

**The GDAL stack** usually arrives with the `geopandas` / `pyogrio` / `shapely` /
`pyproj` wheels and needs nothing extra. If those wheels fail to build (common on
Windows without build tools), use the Docker image instead:

```bash
docker build --target base --tag hrl-pipeline .
docker run --rm -v "$PWD:/work" -w /work hrl-pipeline <submission> --registry ...
```

The image is a convenience only &mdash; it has no production runtime role, no
Azure SDKs, and no credentials.

## Validate a submission

```bash
hrl-pipeline <submission-directory> \
  --registry /path/to/hrl-project-registry/project-id-registry.csv \
  --registry-ref <commit-or-tag the registry is pinned to> \
  --output ./out/<submission-id> \
  --pdf
```

The submission directory contains `submission.json`, one primary spatial file
(`.gpkg`, `.geojson`, or a `.zip` shapefile package), and `_READY`. Validation
checks package safety, geometry (including that coordinates fall within the
expected geographic extent once unprojected, which catches a wrong or missing
source CRS), the pinned LinkML profiles, controlled vocabularies, business
rules, and the pinned project-ID registry. It stages the source read-only and
writes create-only artifacts.

Coordinates are reprojected to the equal-area working CRS (EPSG:3310) on read;
canonical storage and the candidate GeoJSON stay in it. The published public
snapshot's `projects.geojson` is reprojected to WGS84 lon/lat (RFC 7946), which
is what the map and other GeoJSON consumers require; the `projects.gpkg`
download stays in EPSG:3310.

Outputs in `--output`:

| Outcome | Files | Exit code |
| --- | --- | --- |
| `AWAITING_APPROVAL` | `validation-report.json`, `validation-report.html`, `validation-report.pdf`, `canonical-candidate.geojson`, `public-candidate.geojson`, `candidate-manifest.json`, `status.json` | 0 |
| `NEEDS_CORRECTION` | `validation-report.json`, `validation-report.html`, `validation-report.pdf`, `status.json` | 2 |

The JSON report is authoritative. The PDF is the standard human-readable
review and provider-facing copy; HTML is also written for browser viewing.

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
  --canonical /path/to/standardized/canonical-restoration-projects.geojson \
  --public-root /path/to/public-exports/restoration-map/restoration-projects \
  --version 2026-08-24
```

Promotion re-checks every candidate artifact checksum and the schema/registry
provenance against `_APPROVE`, merges only submitted IDs into the canonical
dataset, writes a previously unused immutable public snapshot, updates
`current.json` last, and records a private promotion audit. A failed or
interrupted promotion preserves the previous `current.json`.

## Schema snapshot

The pipeline validates against one immutable `hrl-restoration-schema` snapshot,
currently **v1.3.1**, pinned by commit and checksum under `schema-snapshots/`.
Adopting a new release is an explicit, reviewed change: run
`python scripts/import_schema_snapshot.py vX.Y.Z`, then update
`_SNAPSHOT_RELATIVE_PATH` in `src/hrl_restoration_pipeline/validation.py` and the
`force-include` path in `pyproject.toml`, adjust fixtures, and run `pytest`.
Full procedure and ordering: [`CONTRIBUTING.md` &rarr; "Updating the pinned
schema"](CONTRIBUTING.md#updating-the-pinned-schema).

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
