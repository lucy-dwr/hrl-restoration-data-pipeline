# Validation-worker image release handoff

This repository owns the validation worker source, Dockerfile, image test,
and evidence that a released image resolves to one immutable digest. It does
not own ACR, Azure Container Apps Jobs, their Terraform, or their runtime
identity permissions.

## Release boundary

The publisher is a dedicated Microsoft Entra workload identity used only by
GitHub Actions. It is not the validation worker managed identity. The platform
team must configure it before the release workflow is usable:

| Configuration | Required value |
| --- | --- |
| Federated credential | GitHub Actions OIDC issuer and audience, restricted to this repository's protected `production-image-release` environment or a protected `v*` release-tag subject. |
| Azure role | `AcrPush` at the specific ACR resource scope only. |
| GitHub environment | `production-image-release`, with required reviewers and release tag protection. |
| GitHub variables | `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `ACR_NAME`, and `ACR_LOGIN_SERVER`. |

The existing runtime validation identity remains `AcrPull` only. The publisher
must receive no Terraform, Container Apps Job, storage, queue, or
subscription-level Contributor permission.

## Workflow and handoff

`.github/workflows/release-image.yml` runs only for a `v*` tag or a manually
selected `v*` tag. It verifies that the checked-out commit is exactly that
annotated tag, runs the Python suite and Docker `test` target, exchanges the
GitHub OIDC token for the publisher identity, pushes the image, and records
the digest returned by the registry.

The workflow uploads `image-release.json` as a GitHub Actions artifact. Its
contract is `hrl.image-release/v1` and includes the full digest-pinned
reference, release tag, source commit, package version, and timestamp. For
example:

```json
{
  "schema": "hrl.image-release/v1",
  "image_ref": "acr.example.azurecr.io/hrl-validation-worker@sha256:<64 lowercase hex characters>",
  "source_commit": "<40 lowercase hex characters>",
  "release_tag": "v0.2.0",
  "package_version": "0.2.0"
}
```

A tag is release provenance only. Terraform must use `image_ref`, never the
tag used to create it.

## Terraform handoff

An operator opens a reviewable change in `hrl-azure-infrastructure` that:

1. compares the proposed digest and source commit to `image-release.json`;
2. updates only the relevant job's digest-pinned image reference; and
3. applies through that repository's normal reviewed deployment process.

The workflow has read-only repository permission plus `id-token: write`. It
cannot write repository contents, open or merge pull requests, run Terraform,
or update a Container Apps Job. A successful image push therefore never
silently changes the running workload.

## Local contract check

The handoff generator is deliberately small and independently testable:

```bash
pytest tests/test_image_release.py
```
