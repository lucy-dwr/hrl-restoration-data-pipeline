# Contributing / maintaining `hrl-restoration-data-pipeline`

This is the guide for the **technical maintainer** - the person who keeps
`hrl-pipeline` working. If you just need to *run* the tool as the data operator,
you are in the wrong document: start at
[`hrl-azure-infrastructure/RESTORATION_DATA_WORKFLOW.md`](https://github.com/Healthy-Rivers-and-Landscapes-Science/hrl-azure-infrastructure/blob/main/RESTORATION_DATA_WORKFLOW.md).

For how this repository fits into the whole workflow and who owns what, see
[`MAINTENANCE.md`](https://github.com/Healthy-Rivers-and-Landscapes-Science/hrl-azure-infrastructure/blob/main/MAINTENANCE.md)
and
[`DIVISION_OF_RESPONSIBILITIES.md`](https://github.com/Healthy-Rivers-and-Landscapes-Science/hrl-azure-infrastructure/blob/main/DIVISION_OF_RESPONSIBILITIES.md).

---

## Release model: `main` is the release channel

There are no version tags. The operator's routine is to `git pull` this
repository and reinstall before every submission (see the walkthrough, Part 3).
**So every merge to `main` is what the operator runs next.**

- Do not merge to `main` unless CI is green and you would be comfortable with the
  operator running it that hour.
- Keep risky or half-finished work on a branch. Merge finished, tested work only.
- If `main` is ever known to be broken, tell the operator to hold.

`__version__` (in `src/hrl_restoration_pipeline/__init__.py`) and `version` (in
`pyproject.toml`) should always match. Bump both together when a change is worth
marking; the value is recorded in reports and in `current.json` as
`pipeline_version`, but it is not a release gate.

---

## Setup

```sh
python -m venv .venv && source .venv/bin/activate
python -m pip install -e '.[test]'
pytest
```

You need a GDAL / GEOS / PROJ stack. The `geopandas` / `pyogrio` / `shapely` /
`pyproj` wheels bundle it on most platforms; if they do not build, use the
Docker image (`docker build --target base .`) or install the system libraries
(`libgdal`, `libgeos`, `libproj`).

Run the same suite CI runs:

```sh
pytest                              # Python tests
docker build --target test .        # tests inside the pinned Docker image
```

Both must pass. CI (`.github/workflows/ci.yml`) runs exactly these, on Python
3.11, with no Azure access.

---

## What the tool does

`hrl-pipeline` has two subcommands and no service, no queue, no database. It
stages the source read-only and writes create-only artifacts.

### `hrl-pipeline <submission>` - validate

1. **`ingestion.load_submission`** - reads and checks `submission.json`
   (ten required fields), rejects unsafe ZIP archives (member count, uncompressed
   size, compression ratio, incomplete shapefile package), resolves the primary
   spatial file.
2. **`validation.read_spatial`** - opens the file, **reprojects every
   geometry to EPSG:3310 on read**, and records the source CRS and whether a
   reprojection happened.
3. **`validation.validate_records`** - normalization, LinkML profile
   requirements, controlled vocabularies, business rules (stage-dependent
   requiredness, acreage, funding), lead-entity catalog resolution, and the
   pinned project-ID registry. `_validate_spatial` checks geometry type and
   validity, requires an input CRS (**missing CRS is an error, not a silent
   guess**), and checks that coordinates fall inside California once unprojected.
4. If there are no errors: **`transformation.canonicalize`** adds provenance and
   computes `funding_gap`, **`transformation.publicize`** filters to the public
   profile and to active records, and the candidate GeoJSON, `status.json`, and
   `candidate-manifest.json` are written.
5. **`reporting.write_reports`** writes `validation-report.json` (authoritative)
   and `.html` always, and `.pdf` with `--pdf`.

Exit code `0` = `AWAITING_APPROVAL`, `2` = `NEEDS_CORRECTION`.

### `hrl-pipeline promote <candidate>` - publish

`promotion.promote_local`:

1. **`_approval`** - validates the `_APPROVE` marker: required keys,
   `publication_version` matching `YYYY-MM-DD` or `YYYY-MM-DD-rN` (N &ge; 2), a
   real ISO date, an ISO-8601 `approved_at`.
2. **`_candidate_manifest`** - re-hashes every candidate artifact and
   checks it against `candidate-manifest.json`; checks the manifest is complete.
3. Cross-checks `_APPROVE` &harr; manifest &harr; `status.json` (`submission_id`,
   `AWAITING_APPROVAL` status, schema provenance, registry provenance, manifest
   checksum).
4. **`_already_promoted`** - scans `promotion-audits/` for a run with the
   same manifest checksum; refuses a repeat. The immutable
   `promotion-audits/<version>.json` path must not already exist.
5. **`merge`** - upserts only the submitted project IDs into the canonical
   dataset; records absent from the candidate stay untouched. Re-validates the
   merged dataset.
6. **`publish_local`** - writes the immutable `<version>/` folder:
   `projects.geojson` (**reprojected to WGS84 / CRS84**, RFC 7946),
   `projects.gpkg` (**kept in EPSG:3310**), `projects.csv`, `metadata.json`.
   `_validate_snapshot` re-checks the GeoJSON is CRS84 and in range and the
   GeoPackage is EPSG:3310 **before** the pointer can move.
7. Writes the canonical dataset atomically, writes the promotion audit, then
   **`activate_local_snapshot`** writes `current.json` - guarded by the
   pointer checksum captured before promotion started, so a competing publish is
   detected. A failure anywhere before this leaves the previous `current.json`
   in place.

The operator then uploads the outputs to Azure in a fixed order, `current.json`
last.

---

## Module map

| File | Responsibility |
| --- | --- |
| `cli.py` | Argument parsing; `main()` dispatches `promote` vs validate; orchestrates a validate run |
| `ingestion.py` | `load_submission` - manifest checks, archive safety, primary-file resolution |
| `validation.py` | Schema-snapshot path resolution, CRS constants, `read_spatial`, `validate_records`, `_validate_spatial`, `schema_provenance`, the lead-entity catalog |
| `models.py` | `Finding`, `Repair`, `Report` dataclasses; `Report.status` derives the outcome |
| `registry.py` | `CsvRegistry` - read-only view of the pinned `project-id-registry.csv`, provenance recording |
| `transformation.py` | `canonicalize`, `publicize`, `as_feature_collection` (EPSG:3310 form) |
| `reporting.py` | `write_reports` - JSON / HTML / PDF; humanises schema field names |
| `publication.py` | `merge`, `publish_local`, `activate_local_snapshot`, snapshot validation, checksums |
| `promotion.py` | `promote_local` - the `_APPROVE` gate and the whole promote sequence |

---

## Coordinate reference systems

This is the subtlety that caused the 2026-08 `HRL-001` incident, so it is worth
understanding fully.

| Where | CRS | Why |
| --- | --- | --- |
| Everything internal - validation, canonical dataset, candidate GeoJSON | **EPSG:3310** (California Albers) | Equal-area. The acreage business rules need reliable area measurement. |
| Published `projects.geojson` | **WGS84 / CRS84** (`urn:ogc:def:crs:OGC:1.3:CRS84`), plain lon/lat | RFC 7946. Web maps (MapLibre) and most GeoJSON tools assume lon/lat and break on projected meters. |
| Published `projects.gpkg` download | **EPSG:3310** | GIS users want the equal-area version; GeoPackage carries its CRS explicitly. |

Guards, so a CRS mistake cannot reach the map again:

- **On submission:** an input file with no CRS is an error
  (`crs_required`). Geometry that lands outside `EXPECTED_WGS84_BOUNDS`
  (`-125.0, 32.0, -114.0, 42.2`) once unprojected is an error - this is
  what catches a file that claims the wrong CRS.
- **On promotion:** `_validate_snapshot` re-checks that the published
  `projects.geojson` really is CRS84 lon/lat and in range, and that
  `projects.gpkg` really is EPSG:3310, before `current.json` is written.

If you change any CRS behavior, update `EXPECTED_WGS84_BOUNDS`,
`PUBLIC_GEOJSON_CRS`, and the tests in `test_pipeline.py`
(`test_public_geojson_is_wgs84_lonlat_...`,
`test_publish_rejects_a_snapshot_that_is_not_lonlat_...`) together, and
coordinate with the map (its resolver expects CRS84).

---

## Updating the pinned schema

The pipeline validates against **one immutable schema snapshot**, currently
`hrl-restoration-schema` **v1.3.1**, stored under
`schema-snapshots/hrl-restoration-schema/v1.3.1/` (the YAML plus a
`manifest.json` recording the tag, commit SHA, retrieval time, and file
checksum). `pyproject.toml`'s `force-include` copies that folder into the built
wheel so the installed tool carries it.

To adopt a new schema release, **on a review branch**:

1. `python scripts/import_schema_snapshot.py vX.Y.Z` - resolves the annotated
   tag to a commit, downloads the schema YAML at that commit, writes the new
   snapshot folder and manifest. It refuses to overwrite an existing snapshot.
2. In `src/hrl_restoration_pipeline/validation.py`, update
   `_SNAPSHOT_RELATIVE_PATH` to the new version directory.
3. In `pyproject.toml`, update the `force-include` path to the new version
   directory. (Optionally remove the old snapshot folder; keeping it is
   harmless and is a record.)
4. Adjust test fixtures and expectations for the schema change, run `pytest` and
   the Docker test target.
5. Open a PR describing the schema change and its downstream effect. This is an
   explicit, reviewed change - never silent.

Order matters: the schema must be **released and tagged** in
`hrl-restoration-schema` before you import it here, and the pipeline must be able
to consume it before any producer starts sending data shaped for it. See
[`PIPELINE_INFRA.md` &rarr; "Cross-repository change discipline"](https://github.com/Healthy-Rivers-and-Landscapes-Science/hrl-azure-infrastructure/blob/main/PIPELINE_INFRA.md#cross-repository-change-discipline).

---

## The project-ID registry input

`hrl-pipeline` reads `project-id-registry.csv` from `hrl-project-registry`,
passed as `--registry` and pinned with `--registry-ref` (the operator passes the
short commit). It reads `project_id` and `status` only; it never allocates,
alters, retires, or follows a moving pointer. Registry changes are the
maintainer's responsibility - see that repository's README.

---

## Conventions

- Deterministic and offline. No Azure SDKs, no network calls at runtime, no
  wall-clock dependence in logic (timestamps are recorded, not branched on).
- American spellings.
- Keep the dependency pins in `pyproject.toml` exact. Upgrades are deliberate,
  reviewed changes - a GDAL or LinkML bump can shift validation output.
- Update `CHANGELOG.md` for anything the operator would notice.
- The queue workers, the Azure SDK dependencies, and the image-release workflow
  were removed when the workflow moved to operator-run commands. Do not
  reintroduce them without a real second workflow to justify them.
