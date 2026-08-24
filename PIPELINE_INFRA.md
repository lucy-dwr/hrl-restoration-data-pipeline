# HRL Data Pipeline — Cross-Repository Agent Instructions

## Purpose

These instructions define the shared architecture, repository boundaries, data contracts, and development rules for the Healthy Rivers and Landscapes (HRL) Azure data pipeline and its first public application, the HRL restoration map.

This file has been copied into each relevant repository so that an agent working in any one repository understands the whole system and knows both what belongs there and what does not.

The architecture is intentionally **general at the Azure infrastructure level** so that future HRL scientific data pipelines can use the same patterns. The **restoration-project pipeline is the first implemented data workflow**, and the **HRL restoration map is the first downstream public application**.

---

## Relevant Repositories

### 1. `hrl-restoration-schema`
**Role:** Authoritative data standard

**Code:** https://github.com/lucy-dwr/hrl-restoration-schema

Owns:

- The HRL restoration LinkML schema
- Controlled vocabularies and enums that are part of the data standard
- `RestorationProjectSubmission`
- `RestorationProjectCanonicalRecord`
- `RestorationProjectPublicRecord`
- Valid and invalid examples
- Schema documentation
- Tagged schema releases

Does **not** own:

- Azure infrastructure
- Submission ingestion
- Spatial file handling
- Production data
- Public map code
- Pipeline orchestration

The LinkML schema in this repository is the source of truth. Downstream repositories must consume a **released schema version**, not independently maintain a divergent copy.

---

### 2. `hrl-restoration-data-pipeline`
**Role:** Restoration data-specific ingestion, validation, repair, standardization, merging, reporting, and publication code

This is the primary executable data processing repository.

Owns:

- Python pipeline code
- GeoPackage, GeoJSON, and zipped shapefile ingestion
- Spatial validation
- LinkML validation
- Business rule validation
- Conservative automatic repairs
- Canonical data transformation
- Project ID registry validation
- Merge/upsert logic for the master canonical restoration dataset
- Public data transformation
- HTML and JSON quality reports
- Reference layer acquisition and transformation
- Publication candidate generation
- Publication promotion logic
- Dockerfile and runtime dependencies
- Automated tests

Does **not** own:

- Azure resource definitions
- The authoritative LinkML schema
- Public application UI code
- Production data files in Git

---

### 3. `hrl-azure-infrastructure`
**Role:** Terraform definitions for Azure infrastructure

Owns:

- ADLS Gen2 / Blob Storage
- Storage containers and paths
- Storage Queue resources
- Event Grid subscriptions
- Azure Container Registry
- Azure Container Apps Environment
- Azure Container Apps Jobs
- Managed identities and role assignments
- Log Analytics and diagnostics
- Azure Static Web Apps
- Azure Front Door
- Public data routing
- Terraform state configuration
- Environment-specific infrastructure configuration

Does **not** own:

- Python validation logic
- Schema definitions
- Data repair rules
- Public map application logic

The infrastructure should remain general enough to support future HRL data pipelines beyond restoration projects.

---

### 4. `hrl-restoration-map`
**Role:** Public visualization and download application

**Code:** https://github.com/lucy-dwr/hrl-restoration-map

Owns:

- React/Vite frontend
- MapLibre/deck.gl visualization
- Public data loading
- Filters, search, map interaction, accessibility, downloads, and display logic
- Public application tests
- Data contract compatibility tests

Does **not** own:

- Production restoration data validation 
- LinkML schema authority
- Production data transformation
- Production ingestion
- Production merge/upsert logic
- Production reference layer acquisition

During migration, legacy conversion scripts or checked-in generated datasets may remain temporarily, but the end state is that this application consumes approved Azure-hosted public artifacts.

---

### 5. `hrl-project-registry` (to be created)
**Role:** Authoritative operational registry for program-assigned HRL project IDs

Owns:

- Database schema and migrations for the project-ID registry
- ID allocation, lifecycle, supersession, and audit rules
- Authorized registry-administration workflow
- Immutable CSV, XLSX, and JSON registry export generation
- Registry export manifests and checksums

Does **not** own:

- The LinkML schema or controlled vocabularies
- Submission validation or repair logic
- Azure resource definitions
- Canonical restoration-project records or public map data

The authoritative registry is an access-controlled DWR-managed relational
database. Its infrastructure is provisioned by `hrl-azure-infrastructure`.
The pipeline receives only read access in production and accepts a named
registry export snapshot for local and offline validation.

---

## Supporting / Producer Repositories

Repositories such as:

- `dwr-restoration-spatial-data`
- `misc-restoration-spatial-data`

may prepare or test source datasets, but they are **not runtime dependencies** of the Azure pipeline.

They may produce files that a program lead later uploads to Azure, but Azure ingestion must work regardless of which local repository, GIS project, agency, consultant, or other process produced the submission.

---

# System Architecture

## Submission Pipeline

```text
Authorized program lead
        |
        | uploads via Azure Portal
        v
ADLS Gen2: raw-submissions/
        |
        | upload submission files
        | upload _READY last
        v
Azure Event Grid
        |
        v
Azure Storage Queue
        |
        v
Azure Container Apps Job
        |
        |-- read spatial package
        |-- validate package
        |-- validate geometry
        |-- validate against current LinkML release
        |-- validate business rules
        |-- apply conservative deterministic repairs
        |-- create HTML + JSON report
        |-- create canonical candidate data
        |-- create public candidate data
        |
        +------------------------------+
        |                              |
      FAIL                           PASS
        |                              |
        v                              v
validation-reports/           publication-candidates/
status = NEEDS_CORRECTION     status = AWAITING_APPROVAL
                                       |
                                       | human review in Azure Portal
                                       | upload _APPROVE
                                       v
                               publication promotion job
                                       |
                                       | merge/upsert into master
                                       | canonical dataset
                                       | build public snapshot
                                       v
                              standardized/
                              public-exports/
                                       |
                                       v
                               Azure Front Door
                                       |
                                       v
                              downstream public apps
```

Validation and publication are deliberately separate stages.

A dataset that passes validation must **not automatically replace public production data**.

---

# Human Publication Gate

A passing submission becomes a **publication candidate**.

The pipeline must:

1. Write the validation report as both JSON and HTML
2. Write the normalized canonical candidate
3. Write the derived public candidate
4. Set submission status to `AWAITING_APPROVAL`
5. Stop

An authorized program lead reviews the candidate through the Azure Portal.

The reviewer should be able to inspect or download:

- `validation-report.html`
- `validation-report.json`
- canonical candidate output
- public candidate output
- submission metadata
- recorded automatic repairs
- all warnings

Warnings must be **prominent**, not buried. A publishable report with warnings must clearly say that the submission passed **with warnings**.

To advance the candidate, the reviewer uploads a reviewed JSON marker named:

```text
_APPROVE
```

to the candidate directory. It must identify the reviewed submission and
approval time, for example:

```json
{
  "submission_id": "example-001",
  "approved_by": "reviewer-identifier",
  "approved_at": "2026-08-24T20:00:00Z",
  "candidate_manifest_sha256": "<SHA-256 of candidate-manifest.json>"
}
```

The generated `candidate-manifest.json` records the candidate artifact
checksums plus schema and registry provenance. The marker is a promotion
request, not the sole authorization record. Azure
Storage's Entra-authenticated upload audit trail is the authoritative record
of which reviewer identity created it. The promotion job must confirm that the
marker matches `status.json`, whose status is `AWAITING_APPROVAL`, before it
does any merge or publication work.

Event Grid detects creation of `_APPROVE` and queues the publication/promotion
step. The job revalidates the marker, candidate, schema/registry provenance,
and artifact checksums; it must not trust the event payload alone.

No custom approval web application is required.

A candidate that is never approved remains unpublished.

---

# Submission Upload Contract

Program leads upload submissions through the Azure Portal.

Each submission should have its own directory.

Example:

```text
raw-submissions/
  dwr/
    2026-08-21_dwr_restoration-projects_v001/
      submission.json
      restoration-projects.gpkg
      _READY
```

Supported spatial inputs:

- `.gpkg`
- `.geojson`
- `.zip` containing a complete shapefile package

Do not require users to upload individual shapefile component files separately. Shapefile submissions must be zipped.

`_READY` is always uploaded last.

The ingestion Event Grid rule must react to `_READY`, not every individual data file.

---

## `submission.json`

The submission manifest records **submission-level provenance and processing context** that cannot be reliably derived from the spatial dataset itself. Keep the manifest intentionally small. Dataset attributes and metadata governed by the LinkML schema belong in the dataset, not in this file.

### Required fields

A submission manifest should include:

```json
{
  "submission_id": "2026-08-24_dwr_restoration-projects_v001",
  "organization": "California Department of Water Resources",
  "organization_code": "DWR",
  "dataset_name": "HRL Restoration Projects",
  "submission_type": "update",
  "submission_scope": "complete_organization_snapshot",
  "data_as_of": "2026-08-20",
  "primary_file": "restoration-projects.gpkg"
}
```

Required fields have the following meanings:

| Field | Purpose |
| --- | --- |
| `submission_id` | Stable identifier tying together the raw submission, validation reports, publication candidate, logs, and publication history. |
| `organization` | Human-readable name of the organization supplying the data. |
| `organization_code` | Short, stable organization identifier suitable for storage paths and machine processing, such as `DWR` or `CDFW`. |
| `dataset_name` | Human-readable name of the submitted dataset. |
| `submission_type` | Identifies whether the submission is a routine `update` or a `correction` to a previous submission. |
| `submission_scope` | Indicates whether the submission is a `complete_organization_snapshot` or a `partial_update`. |
| `data_as_of` | Date through which the submitter considers the data current. This is distinct from the upload or processing date. |
| `primary_file` | Name of the spatial file or archive the pipeline should process. |

### Submission types

Initially support:

```text
update
correction
```

An `update` contains new or updated records to be merged/upserted into the canonical dataset.

A `correction` revises a previous submission. When `submission_type` is `correction`, `supersedes_submission_id` must also be supplied.

### Submission scope

Initially support:

```text
complete_organization_snapshot
partial_update
```

A `complete_organization_snapshot` indicates that the submission represents the organization's complete applicable dataset as of `data_as_of`.

A `partial_update` contains only records being added or changed.

Submission scope provides important provenance but **does not change the default deletion rule**: absence of a record from a later submission must never be interpreted as deletion or retirement. Deletion or retirement requires an explicit future mechanism.

### Optional fields

Optional submission-level provenance may include:

```json
{
  "supersedes_submission_id": "2026-08-21_dwr_restoration-projects_v001",
  "submitted_by": {
    "name": "Jane Smith",
    "email": "jane.smith@water.ca.gov"
  },
  "notes": "Quarterly project status update.",
  "known_limitations": [
    "Two project boundaries are still under review."
  ]
}
```

`supersedes_submission_id` is required for a `correction` and otherwise optional.

`submitted_by`, `notes`, and `known_limitations` should remain optional. Do not require users to manually supply information that Azure or the pipeline can reliably determine automatically.

### Derived metadata

Do **not** require the uploader to provide metadata that the system can reliably derive, including:

- upload or processing timestamp
- file format
- CRS
- geometry type
- feature count
- expected fields
- validation status
- pipeline version
- schema version

The system validates each submission against the **current approved production schema release**. Uploaders do not select a schema version.

The validation report and downstream provenance metadata must record at minimum:

- schema name
- schema version
- pipeline version
- validation timestamp
- submission ID

As a general rule:

> **If the system can reliably determine a metadata value, derive and record it automatically rather than requiring the uploader to enter it.**
---

# Raw Data Is Immutable

Never overwrite or mutate a submitted source file.

The raw submission is the provenance record.

If a submitter needs to make corrections, create a new submission version.

Example:

```text
2026-08-21_dwr_restoration-projects_v001/
2026-08-22_dwr_restoration-projects_v002/
```

Automatic repairs must be applied to a working/candidate representation, never silently written back into the raw submission.

---

# Storage Model

The Azure data layer should distinguish at least these logical areas:

```text
raw-submissions/
standardized/
validation-reports/
publication-candidates/
schema-snapshots/
registry-exports/
public-exports/
```

The infrastructure may implement these as containers or appropriately isolated paths, but security and lifecycle boundaries should remain clear.

---

## Schema Snapshots

```text
schema-snapshots/
  hrl-restoration-schema/
    v1.1.1/
      hrl_restoration_project.yaml
      manifest.json
```

The HRL restoration schema repository remains the authoritative source of
truth. A pipeline release consumes an exact released schema snapshot, rather
than a live branch, Git submodule, or runtime GitHub dependency. The snapshot
must be copied from the upstream release without local modification.

`manifest.json` must record at minimum the upstream repository, released tag,
immutable commit SHA, retrieval timestamp, and SHA-256 checksum of each
artifact. The pipeline uses native LinkML tooling against this snapshot.

Upgrading the production schema is an explicit, reviewable change: import the
new released snapshot, update the configured production schema version, run
contract and pipeline tests, and deploy a new pipeline image. The pipeline
must never automatically follow new tags or a moving branch. Validation
reports and publication metadata must record the exact schema version and
checksum used.

Snapshot import must be automated, not manually copied. When the schema
repository publishes a release tag, its release workflow must resolve the tag
to an immutable commit SHA, copy the released schema artifact into the pipeline
repository's versioned snapshot path, generate the provenance/checksum
manifest, and open a pull request. The pull request must include any required
contract-test updates. A maintainer must review and merge it before the new
schema is adopted by a pipeline build or deployed to production; release tags
must never change production validation automatically.

Schema releases and project-ID registry updates are independent. The schema
defines the structure, required fields, and controlled vocabularies; the
registry governs allocation and lifecycle of program-assigned IDs. A schema
release must not silently alter registry records, and a registry update must
not require a schema release unless the registry contract itself changes.

---

## Project-ID Registry Exports

```text
registry-exports/
  project-id-registry/
    2026-08-24T174500Z/
      project-id-registry.csv
      project-id-registry.xlsx
      project-id-registry.json
      manifest.json
      SHA256SUMS
    current.json
```

The authoritative project-ID registry is an access-controlled DWR-managed
relational database. It is the only system that allocates, changes lifecycle
status for, or retires program-assigned IDs. The registry must preserve
immutable IDs, record status and supersession deliberately, and retain an
audit trail of changes. The ingestion pipeline has read-only access and must
never create or modify registry records.

The registry produces immutable, versioned export snapshots for people and
systems that do not work in Azure. CSV and XLSX exports support email and
spreadsheet use; JSON supports system use. These are read-only reference
artifacts, not an update channel. They must include a conspicuous statement
that recipients must request allocations or corrections through the HRL
program registry process rather than editing and returning the file.

Exports must contain only shareable registry fields, such as `project_id`,
status, project name, assigned organization code, and assigned date. Internal
notes, user identities, and audit history must not be included. Each export
manifest records the export version, timestamp, row count, database migration
version, checksums, and source registry identity. `current.json` points to the
most recent approved export.

Production validation queries the authoritative registry. Local and offline
validation may use a specific exported CSV or JSON snapshot. In either case,
the validation report must record the registry source, export version when
applicable, and checksum so that the result is reproducible.

---

## Raw Submissions

```text
raw-submissions/
```

Contains original uploaded files and manifests.

Properties:

- Private
- Immutable by pipeline convention
- Retained for provenance
- Never served to public applications

---

## Validation Reports

```text
validation-reports/
```

Contains:

- `validation-report.json`
- `validation-report.html`
- `validation-report.pdf`
- status metadata
- pipeline version
- schema version
- warnings
- errors
- repairs

JSON is the authoritative machine-readable report.

HTML and PDF are the human-readable representations.

---

## Publication Candidates

```text
publication-candidates/
```

Contains outputs that passed validation but have not yet been approved.

Example:

```text
publication-candidates/
  restoration-projects/
    <submission-id>/
      canonical-candidate.geojson
      public-candidate.geojson
      validation-report.json
      validation-report.html
      status.json
      _APPROVE
```

The `_APPROVE` marker is absent until a program lead approves promotion.

---

## Standardized Data

```text
standardized/
```

Contains the internal canonical HRL dataset.

This may contain fields not intended for public applications.

For restoration projects, this is the merged/upserted canonical master dataset conforming to `RestorationProjectCanonicalRecord`.

---

## Public Exports

```text
public-exports/
```

Contains approved, privacy-filtered, application-ready data.

Public exports must exclude fields that are not approved for public release, including internal or contact fields where applicable.

The public export is derived from canonical standardized data. Canonical and public data are not the same thing.

For restoration projects, the public export conforms to
`RestorationProjectPublicRecord`. It excludes contacts, contractors, funding
secured, funding gap, all comment fields, and canonical provenance fields.
Funding sources remain public.

---

# Validation Model

Validation should be staged.

## 1. Package Validation

Examples:

- unreadable file
- missing shapefile components
- unsupported format
- multiple ambiguous primary layers
- corrupt archive

Package failures are errors.

---

## 2. Spatial Validation

Examples:

- missing CRS
- invalid geometry
- unexpected geometry type
- empty geometry
- geometry outside allowed geographic extent, if such a rule is adopted
- reprojection requirements

Safe reprojection may be automatic.

Ambiguous spatial problems must fail.

---

## 3. LinkML Schema Validation

Validate against the **current production release** of the HRL restoration schema.

Use native LinkML tooling from Python.

Do not independently reproduce the LinkML schema as a second manually maintained validation system.

---

## 4. Controlled Vocabulary Validation

Controlled vocabularies come from the schema release.

Unknown or invalid values must be handled according to the repair rules below.

---

## 5. Business Rule Validation

Examples may include:

- invalid combinations of fields
- impossible numeric values
- inconsistent project state
- invalid date relationships
- missing program-required values that cannot be expressed directly in LinkML

Business rules belong in the pipeline repository unless they are genuinely part of the reusable schema standard.

For restoration projects, each submitted record must be a complete record, not
a field-level patch. Each submitted `project_id` is a stable, program-assigned
string and must exist in the central DWR-managed project-ID registry. A new
project must be registered and assigned an ID through the program's intake
process before it can pass validation; the ingestion job must not create IDs
automatically. Production checks use the authoritative registry database.
Local or offline checks may use a named immutable registry export snapshot,
whose version and checksum must be recorded in the validation report.

The schema identifies the fields required on every project record. The
pipeline applies stage-dependent rules: missing contact details are warnings;
budget, secured funding, and construction years may be absent with warnings
through design, but are errors at construction and post-construction stages.
Total acreage is required except for projects that are exclusively fish passage
and/or fish screen work.

---

# Error, Warning, and Repair Semantics

The pipeline must distinguish:

## Error

An issue that prevents publication.

Examples:

- required field missing
- ambiguous controlled vocabulary value
- invalid required geometry
- incompatible field type

Result:

```text
NEEDS_CORRECTION
```

No candidate may be approved or published.

---

## Warning

An issue that does not prevent publication but must be prominent in the report.

Examples:

- potentially unusual but valid value
- non-critical metadata issue
- accepted geometry repair
- value that merits human attention

Warnings may proceed to `AWAITING_APPROVAL`.

Warnings must be visually obvious in the HTML and PDF reports and clearly counted in status metadata.

---

## Automatic Repair

Automatic repairs must be:

- deterministic
- conservative
- semantics-preserving
- fully recorded
- reproducible

Examples:

- trim whitespace
- normalize casing where the standard defines case insensitivity
- replace a known controlled vocabulary alias with the canonical value
- normalize date formatting
- reproject geometry to the required CRS
- repair geometry only where the repair is deterministic and does not materially alter meaning

Never guess an ambiguous value.

Never silently repair data.

Each repair record should include:

- feature or record identifier
- field or geometry affected
- original value
- repaired value
- repair rule
- pipeline version

---

# Canonical Master Dataset and Merge/Upsert Behavior

Passing submissions do not replace the entire restoration dataset.

They are merged/upserted into a canonical master dataset using stable `project_id` values.

The canonical record must preserve record-level provenance for the most recent
approved source, including the source project identifier, organization code,
submission ID, and source data-as-of date. The canonical model must also have
an explicit record status field so retirement or supersession is intentional,
never inferred from a record being absent from a later submission.

Conceptually:

```text
current canonical master
        +
approved submission candidate
        |
        v
deterministic merge/upsert
        |
        v
new canonical master snapshot
```

The pipeline must define explicit behavior for:

- new `project_id`
- existing `project_id`
- unchanged record
- changed record
- records absent from a later agency submission
- deliberate deletion or retirement
- conflicting submissions
- duplicate IDs
- invalid attempts to change program-assigned identifiers

Do **not** infer deletion merely because a record is absent from a later submission.

If record deletion/retirement is needed, represent that intentionally through an explicit field or future workflow.

The merge must be deterministic and testable.

---

# Publication Model

Publication must use immutable snapshots.

Do not repeatedly overwrite a single production GeoJSON as the only source of truth.

Example:

```text
public-exports/
  restoration-projects/
    2026-08-21T153422Z/
      projects.geojson
      projects.gpkg
      projects.csv
      metadata.json

    current.json
```

`current.json` points to the approved current snapshot.

Example:

```json
{
  "version": "2026-08-21T153422Z",
  "geojson": "2026-08-21T153422Z/projects.geojson",
  "gpkg": "2026-08-21T153422Z/projects.gpkg",
  "csv": "2026-08-21T153422Z/projects.csv",
  "schema_version": "1.1.0",
  "pipeline_version": "1.4.2"
}
```

Versioned artifacts should be effectively immutable.

`current.json` is the promotion pointer.

Rollback should be possible by repointing `current.json` to a previously approved snapshot.

---

# Public Application Contract

The HRL restoration map should eventually fetch:

```text
current.json
```

then fetch the versioned GeoJSON referenced by that manifest.

The application must not know how raw submissions are validated or merged.

The frontend's contract is:

> consume already-approved public artifacts.

Production application code should not depend on a checked-in production GeoJSON once migration is complete.

Small test fixtures may remain in Git for automated tests.

---

# Reference / Context Data Pipeline

Reference layers are not submission data.

Examples include:

- watershed boundaries
- Delta boundary
- bypass boundaries
- stream network
- future authoritative context layers

They follow a separate workflow:

```text
authoritative external source
        |
        v
retrieve
        |
        v
validate source response
        |
        v
transform / clip / dissolve / simplify
        |
        v
validate output
        |
        v
versioned publication candidate
        |
        v
publish successful snapshot
```

These workflows should live in `hrl-restoration-data-pipeline`, under a clearly separate reference-data module.

They may use the same container image but different entry points.

---

## Reference Layer Execution

Initially, reference layer jobs should be **manually triggered** Container Apps Jobs.

Add schedules later only when source update frequency and operational value justify them.

Different reference layers may eventually use different schedules.

---

## Reference Layer Failure Rule

A failed reference-layer refresh must never damage the currently published layer.

On failure:

- record the failure
- log diagnostic information
- leave the current production pointer unchanged
- do not overwrite the last successful artifact

Only a successfully processed and validated new snapshot may replace the current pointer.

---

## Reference Layer Versioning

Version reference layers too.

Example:

```text
public-exports/
  reference/
    watersheds/
      2026-08-21/
        watersheds.geojson
        metadata.json

    streams/
      nhdplus-v2-build-2026-08/
        streams.pmtiles
        metadata.json
```

For every published reference layer, preserve provenance metadata such as:

- authoritative source
- retrieval time
- source version if known
- source endpoint or dataset identifier
- pipeline version
- transformations applied
- relevant spatial parameters
- output checksum if useful

---

# Stream-Network Processing

The stream network may require tools such as `tippecanoe`.

Do not reimplement specialized tooling in Python merely to avoid a subprocess.

The pipeline container may include:

- Python
- GDAL / PROJ
- GeoPandas
- Shapely
- Pyogrio
- LinkML
- Azure SDK packages
- `tippecanoe`
- other justified geospatial CLI dependencies

Python remains the orchestration and application language.

---

# Schema Release Contract

`hrl-restoration-schema` owns schema releases.

The production pipeline must validate against the current approved production schema release.

The pipeline must record:

- schema name
- schema version
- pipeline version
- validation time

The validator must not dynamically use whatever happens to be on the schema repository's `main` branch.

Use a tagged release or otherwise immutable released artifact.

A schema update and a pipeline update are separate versioned changes and must remain independently traceable.

---

# Container and Release Contract

`hrl-restoration-data-pipeline` builds the production container image.

GitHub Actions in that repository should:

1. install dependencies
2. run formatting/linting as adopted
3. run unit tests
4. run integration tests
5. run representative valid/invalid spatial fixtures
6. build the Docker image
7. tag the image with an immutable version or commit identifier
8. push the image to Azure Container Registry

Production must not depend on a mutable `latest` tag.

Terraform remains authoritative for which image version production executes.

A merge to the pipeline repository should not silently switch the production Container Apps Job to a newly built image.

---

# Azure Infrastructure Responsibilities

The `hrl-azure-infrastructure` repository should implement the shared runtime.

Expected production infrastructure includes:

```text
rg-hrl-data-prod-wus3
  ADLS Gen2 / Blob Storage

rg-hrl-pipelines-prod-wus3
  Azure Container Registry
  Container Apps Environment
  submission processing job
  publication promotion job
  reference-layer job(s)
  Storage Queue
  Event Grid subscriptions
  managed identity
  diagnostics

rg-hrl-apps-prod-wus3
  Azure Static Web Apps
  Azure Front Door
```

Exact resource grouping should remain aligned with the existing Terraform architecture.

Use managed identities and Azure RBAC rather than embedded secrets wherever supported.

Do not put credentials, storage keys, SAS tokens, Terraform state, or real `.tfvars` files in Git.

---

# Identity and Upload Model

Do not build custom uploader identity management at this stage.

Authorized program leads already have Azure access.

Uploads are performed directly through the Azure Portal.

The Azure access model should rely on existing Azure RBAC and program-controlled contributor/data roles.

Do not create:

- custom upload web application
- custom user database
- custom login flow
- upload API solely for this workflow
- email notification system

unless requirements later change.

---

# Status Model

Use explicit status values.

Recommended states:

```text
UPLOADING
READY
VALIDATING
NEEDS_CORRECTION
AWAITING_APPROVAL
APPROVED
PUBLISHING
PUBLISHED
FAILED
```

Reference workflows may use a simpler analogous status model.

Status transitions should be deterministic and recorded.

---

# Observability

Every job execution should log enough information to diagnose failures without inspecting source code.

Include at minimum:

- submission ID
- job/execution ID
- pipeline version
- schema version where applicable
- input path
- output path
- validation status
- error counts
- warning counts
- repair counts
- exception details
- publication snapshot version

Do not log sensitive submission contents unnecessarily.

---

# Testing Requirements

## Schema Repository

Test:

- schema validity
- generated artifacts
- valid examples
- invalid examples
- controlled vocabularies
- release reproducibility

---

## Pipeline Repository

Test:

- GPKG ingestion
- GeoJSON ingestion
- zipped shapefile ingestion
- corrupt package behavior
- CRS handling
- invalid geometry
- LinkML validation
- controlled vocabulary repair
- ambiguous vocabulary failure
- warning behavior
- canonical transformation
- public-field removal
- deterministic merge/upsert
- duplicate IDs
- publication candidate generation
- approval promotion
- snapshot generation
- rollback-safe pointer updates
- reference API failure
- malformed reference responses
- reference publication safety

Use small test fixtures committed to Git. Do not commit production datasets.

---

## Infrastructure Repository

Test:

- `terraform fmt`
- `terraform validate`
- plans in appropriate CI contexts
- Event Grid filters
- queue configuration
- Container Apps Job configuration
- managed identity role assignments
- storage access boundaries
- Front Door routing

Do not apply production infrastructure automatically from unreviewed pull requests.

---

## Map Repository

Test:

- `current.json` parsing
- versioned GeoJSON loading
- failure handling
- backward-compatible data contracts
- downloads
- expected fields
- accessibility
- deployment path behavior

The frontend should fail visibly and safely if a published manifest is malformed or unavailable.

---

# Cross-Repository Change Rules

Agents must consider whether a change modifies a contract consumed by another repository.

Examples:

### Schema field changes

May require changes in:

- pipeline validation
- canonical transformation
- public export logic
- map display logic
- test fixtures

---

### Public manifest changes

May require changes in:

- pipeline publisher
- map data loader
- infrastructure routing/caching
- tests

---

### Storage path changes

May require changes in:

- Terraform
- pipeline storage client
- documentation
- Event Grid filters

---

### Container command changes

May require changes in:

- pipeline repo
- Terraform Container Apps Job definitions

---

Before implementing a cross-repository contract change, identify all affected repositories and keep the contract explicit.

---

# Migration From Current Map-Bundled Data Processing

The existing restoration map currently includes data conversion and reference layer scripts.

Migrate production responsibilities gradually.

End state:

```text
hrl-restoration-map
  renders data

hrl-restoration-data-pipeline
  produces data
```

Do not delete legacy scripts until equivalent pipeline behavior is tested and the Azure-hosted path is proven.

During migration, keep clear documentation of which path is authoritative.

---

# Development Order

Recommended implementation sequence:

## Phase 1 — Schema Contract

1. Confirm `hrl-restoration-schema` v1.1.1 as the current production release.
   It documents that `project_id` is program-assigned before submission and
   required on every submitted record, consistent with the LinkML source.
2. Confirm canonical record structure and stable `project_id` behavior.
3. Add or improve valid and invalid fixtures.
4. Import the exact released schema into a pipeline schema snapshot with its
   release tag, commit SHA, and checksums.
5. Define the project-ID registry database and export contracts, including a
   minimal immutable test export. This does not require deploying Azure
   infrastructure yet.

---

## Phase 2 — Local Pipeline

1. Add Python project configuration, a pinned geospatial dependency set, and
   the exact released LinkML schema snapshot.
2. Implement local GeoPackage, GeoJSON, and zipped-shapefile ingestion.
3. Implement staged package, spatial, LinkML, controlled-vocabulary, and
   business-rule validation.
4. Define a `ProjectIdRegistry` interface and implement an immutable
   CSV/JSON-export adapter for local and offline validation. Do not implement
   ID allocation in the pipeline.
5. Implement conservative, deterministic repair rules with complete repair
   records.
6. Generate JSON and HTML reports that record the schema and registry
   versions/checksums used.
7. Generate canonical and public candidate outputs.
8. Implement deterministic merge/upsert logic and immutable local publication
   snapshots.
9. Add fixtures and tests for valid and invalid packages, geometry, schema
   values, repairs, registry status and missing IDs, reports, merge behavior,
   and publication rollback safety.

Do not begin with Azure orchestration. First make the pipeline deterministic and testable locally.

---

## Phase 3 — Containerization

1. Create Dockerfile.
2. Include geospatial system dependencies.
3. Include LinkML tooling.
4. Include `tippecanoe` if required.
5. Run the full test suite in the container.
6. Build and push immutable image tags to ACR.

---

## Phase 4 — Azure Pipeline Infrastructure

Implement `infra/environments/prod/pipelines`. This repository owns the
restoration-specific validation and promotion behavior; the infrastructure
repository owns the resources, permissions, triggers, and observability below.

Provision:

- ACR
- Container Apps Environment
- Azure SQL Database for the authoritative project-ID registry
- Private database connectivity, Microsoft Entra ID authentication, backups,
  auditing, and least-privilege managed-identity access
- Separate private storage paths for raw submissions, validation reports,
  publication candidates, and canonical standardized data; expose only
  approved immutable public-export artifacts through the public route
- Storage Queue with retry and dead-letter handling
- Event Grid `BlobCreated` filters scoped to the exact `_READY` and `_APPROVE`
  paths; `_APPROVE` events must be sent to the promotion queue, not invoke a
  job directly
- submission job
- approval/promotion job
- registry export job and private versioned `registry-exports/` storage
- distinct managed identities and least-privilege RBAC:
  - submitters may upload only raw submissions;
  - reviewers may read candidate/report artifacts and create approval markers;
  - the validation identity may read raw submissions and registry data and
    write reports/candidates, but may not allocate or alter registry IDs;
  - the promotion identity may read approved candidates, write standardized
    data and new public snapshots, and update `current.json`;
  - registry administrators remain separate from both pipeline identities;
  - rollback/operator permissions are separate from routine reviewer access
- Blob versioning, soft delete, retention/immutability controls appropriate to
  raw submissions and completed versioned snapshots, plus storage diagnostics
  that retain the Entra identity that created approval markers
- diagnostics, queue monitoring, and alerts for failed validation, failed
  promotion, retries, and dead-lettered events

Wire `_READY` to validation.

Wire `_APPROVE` creation to the promotion queue. Event delivery is at least
once, so the promotion job and storage layout must make duplicate deliveries
safe and must serialize competing promotions.

For promotion, provision the storage semantics needed to:

1. Write every new public artifact under a previously unused immutable version
   prefix.
2. Validate checksums before making it current.
3. Update `current.json` last with an ETag/conditional write, so concurrent
   promotions cannot silently replace one another.
4. Preserve the prior `current.json` when a write or validation fails.
5. Record a promotion result/audit artifact without publishing private
   candidate or canonical fields.

Deploy the registry schema and grant the ingestion job read-only registry
access. Registry-administration identities must be separate from pipeline
identities. The export job may read the registry but must only write immutable
registry snapshots and the `current.json` pointer.

---

## Phase 5 — Azure Data Flow

Prove:

```text
Portal upload
  -> _READY
  -> validation
  -> report
  -> AWAITING_APPROVAL
  -> human review
  -> _APPROVE
  -> merge/upsert
  -> public snapshot
  -> current.json
```

Test both passing and failing submissions.

Test publication with warnings.

Test that failed publication leaves the previous public version intact.

Test that an approval marker uploaded by an unauthorized identity cannot be
created or cannot trigger usable promotion, and that the authorized upload is
traceable in storage diagnostics.

Test duplicate and out-of-order Event Grid deliveries, promotion queue retry
and dead-letter behavior, and concurrent promotion attempts. No scenario may
publish a partial snapshot or silently overwrite a newer `current.json`.

Test that the promotion job rejects a missing, malformed, mismatched, or
non-`AWAITING_APPROVAL` marker before it writes canonical or public data.

Test production registry validation against the database, as well as
reproducible local validation against a specific immutable export snapshot.
Test that a registry export failure leaves the prior export pointer unchanged.

---

## Phase 6 — Reference Layers

Move or reimplement production reference layer generation in the pipeline repository.

Start with:

- watersheds
- Delta boundary
- bypass boundaries
- stream network

Use manually triggered jobs initially.

Preserve source metadata and immutable snapshots.

---

## Phase 7 — Map Migration

Modify `hrl-restoration-map` to consume Azure-hosted manifests and versioned public artifacts.

Retain local test fixtures.

Remove production dependence on checked-in generated restoration data only after the Azure path is verified.

---

# General Rules

1. Keep repository boundaries strict.
2. Prefer explicit data contracts over implicit coupling.
3. Preserve raw data and provenance.
4. Never silently repair data.
5. Never publish a failed validation.
6. Make warnings prominent.
7. Never let a failed refresh replace a known-good public artifact.
8. Use immutable snapshots and mutable pointers/manifests.
9. Keep schema and pipeline versions independently traceable.
10. Do not use mutable container tags in production.
11. Do not introduce a database unless a concrete requirement justifies it.
12. Do not introduce Data Factory, Databricks, AKS, Service Bus, or other heavier services without a specific need.
13. Prefer the simplest PaaS component that satisfies the requirement.
14. Do not build custom identity or upload UI unless requirements change.
15. Keep Azure infrastructure generic enough to support future HRL pipelines.
16. Keep restoration-specific business logic out of generic Terraform modules.
17. Do not commit production data or secrets to Git.
18. Make local pipeline behavior testable without Azure.
19. Treat public data transformation as a deliberate release step, not an incidental file conversion.
20. When uncertain about a cross-repository contract, document the decision before coding around it.

---

# Target End State

```text
hrl-restoration-schema
        |
        | released LinkML schema
        v
hrl-restoration-data-pipeline
        |
        | tested immutable container image
        v
hrl-azure-infrastructure
        |
        | runs validation / approval / publication
        v
ADLS + versioned public exports
        |
        v
Azure Front Door
        |
        v
hrl-restoration-map
```

The architectural principle is:

> **GitHub governs schemas, code, and infrastructure. Azure receives and preserves data, executes released pipeline code, records validation and provenance, and publishes approved immutable data products. Public applications consume only approved published artifacts.**
