# Agent instructions: `hrl-restoration-data-pipeline`

Deterministic, local validation and promotion for HRL restoration project
submissions. The HRL data operator runs `hrl-pipeline` and `hrl-pipeline
promote` as part of the email-to-publication workflow. No Azure orchestration,
no queue workers, no database.

- Cross-repository context:
  [`hrl-azure-infrastructure/PIPELINE_INFRA.md`](https://github.com/Healthy-Rivers-and-Landscapes-Science/hrl-azure-infrastructure/blob/main/PIPELINE_INFRA.md).
- How to maintain this repository: [`CONTRIBUTING.md`](CONTRIBUTING.md).
- Roles and ownership:
  [`hrl-azure-infrastructure/DIVISION_OF_RESPONSIBILITIES.md`](https://github.com/Healthy-Rivers-and-Landscapes-Science/hrl-azure-infrastructure/blob/main/DIVISION_OF_RESPONSIBILITIES.md).

## Rules

1. **`main` is the release channel.** The operator pulls `main` and reinstalls
   before every run. Keep `main` green and deployable; land risky work on a
   branch.
2. Deterministic and offline. No Azure SDKs, no runtime network calls, no
   branching on wall-clock time. Tests use a synthetic registry fixture and the
   pinned schema snapshot.
3. The pinned schema snapshot (`schema-snapshots/hrl-restoration-schema/vX.Y.Z/`)
   is immutable. Changing it is the explicit, reviewed procedure in
   `CONTRIBUTING.md` - `import_schema_snapshot.py`, then
   `_SNAPSHOT_RELATIVE_PATH` in `validation.py` and the `force-include` in
   `pyproject.toml`, then fixtures and `pytest`.
4. Internal geometry is EPSG:3310. The published `projects.geojson` is WGS84 /
   CRS84 (RFC 7946); `projects.gpkg` stays EPSG:3310. A missing input CRS is an
   error; geometry outside California once unprojected is an error; promotion
   re-checks the published snapshot before moving `current.json`.
5. Validation never publishes. Promotion runs only against an explicit
   `_APPROVE` marker whose checksum matches the candidate.
6. American spellings. Keep dependency pins exact. Update `CHANGELOG.md` for
   anything the operator would notice.
7. Run `pytest` and `docker build --target test .` before finishing.

## Layout

```text
src/hrl_restoration_pipeline/
  cli.py            argument parsing and run orchestration
  ingestion.py      submission.json + archive safety + primary-file resolution
  validation.py     schema snapshot, CRS handling, record + spatial validation
  transformation.py canonicalize / publicize / feature-collection forms
  reporting.py      JSON / HTML / PDF reports
  publication.py    merge, publish_local, activate_local_snapshot, checksums
  promotion.py      promote_local: the _APPROVE gate and the promote sequence
  models.py         Finding / Repair / Report
  registry.py       CsvRegistry: read-only pinned project-id-registry.csv
scripts/import_schema_snapshot.py   pull one released schema tag into the store
schema-snapshots/hrl-restoration-schema/v1.3.1/   the pinned schema + manifest
tests/test_pipeline.py     the whole suite; deterministic, offline
tests/fixtures/registry.csv  synthetic registry
Dockerfile           optional GDAL image; base + test targets; no runtime role
.github/workflows/ci.yml   pytest (3.11) + docker test target, no Azure
```
