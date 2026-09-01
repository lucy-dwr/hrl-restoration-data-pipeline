# Changelog

Notable changes to `hrl-restoration-data-pipeline`, newest first.

This project does not tag releases: `main` is the channel the operator runs (see
[`CONTRIBUTING.md`](CONTRIBUTING.md)). `__version__` is bumped when a change is
worth marking; entries here are dated. Backfilled from the git history up to
2026-08-31.

## Unreleased

_Nothing yet._

## 2026-08-31 &mdash; public snapshot CRS fix (`0.3.0`)

### Fixed

- The published `projects.geojson` was written in EPSG:3310 projected metres
  instead of WGS84 longitude/latitude, so every feature landed far outside the
  map's view. `hrl-pipeline promote` now reprojects the published
  `projects.geojson` to WGS84 / CRS84 per RFC 7946; `projects.gpkg` stays in
  EPSG:3310.

### Added

- Submission validation requires an input CRS (a missing CRS is an error, not a
  silent guess) and rejects geometry that falls outside California once
  unprojected (`EXPECTED_WGS84_BOUNDS`).
- Promotion re-checks that the published `projects.geojson` is CRS84 lon/lat and
  in range, and that `projects.gpkg` is EPSG:3310, before `current.json` is
  written.
- `artifact_crs` is recorded in the snapshot `metadata.json`.

## 2026-08-28 &mdash; promotion hardening on real data

### Fixed

- Promotion failed on a real submission because of `NaN` values in multivalued
  fields and an oversized geometry. The public snapshot writer and the
  multivalue handling were cleaned up and corrected.

## 2026-08-27 &mdash; simplification to an operator-run tool

### Removed

- The queue-consuming `hrl-validation-worker` and `hrl-promotion-worker`
  entrypoints, the Azure SDK dependencies, and the container image-release
  workflow. The workflow is now the operator running `hrl-pipeline` and
  `hrl-pipeline promote` locally; there is no Azure job, queue, or registry.

### Changed

- The pipeline is pinned to `hrl-restoration-schema` **v1.3.1** by immutable
  commit and checksum under `schema-snapshots/`.

## 2026-08-24 &mdash; initial pipeline

### Added

- Deterministic local validation: package safety, geometry, coordinate system,
  LinkML profile conformance, controlled vocabularies, business rules, and the
  pinned project-ID registry. JSON, HTML, and PDF reports.
- Candidate generation (`canonical-candidate.geojson`,
  `public-candidate.geojson`, `candidate-manifest.json`, `status.json`).
- `hrl-pipeline promote`: the `_APPROVE` gate, checksum verification, canonical
  merge of submitted IDs only, immutable public snapshot, conditional
  `current.json` update with pointer-checksum guard, private promotion audit.
- The immutable schema-snapshot store and `scripts/import_schema_snapshot.py`.
- Python 3.11 test suite and an optional GDAL Docker image.
