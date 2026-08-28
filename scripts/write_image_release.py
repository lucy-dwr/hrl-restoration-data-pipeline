#!/usr/bin/env python3
"""Write the immutable image-release handoff consumed by infrastructure review."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


SHA256_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
COMMIT_SHA = re.compile(r"[0-9a-f]{40}\Z")
RELEASE_TAG = re.compile(r"v[0-9][0-9A-Za-z._-]*\Z")

WORKER_RUNTIME_CONTRACT = {
    "validation": {
        "command": ["hrl-validation-worker"],
        "arguments": [
            "--queue", "validation-requests",
            "--raw-container", "raw-submissions", "--raw-prefix", "",
            "--reports-container", "validation-reports",
            "--candidates-container", "publication-candidates",
            "--registry-container", "registry-exports",
            "--registry-prefix", "project-id-registry/<immutable-version>/",
        ],
    },
    "promotion": {
        "command": ["hrl-promotion-worker"],
        "arguments": [
            "--queue", "promotion-requests",
            "--candidates-container", "publication-candidates",
            "--standardized-container", "standardized",
            "--public-container", "public-exports",
        ],
    },
}


def image_release(
    *,
    image: str,
    digest: str,
    source_commit: str,
    release_tag: str,
    package_version: str,
    created_at: str,
) -> dict[str, str]:
    """Validate and construct the versioned image-release handoff."""
    if not image or "@" in image or image.endswith(":"):
        raise ValueError("image must be a repository name without a tag or digest")
    if not SHA256_DIGEST.fullmatch(digest):
        raise ValueError("digest must be a lowercase sha256 digest")
    if not COMMIT_SHA.fullmatch(source_commit):
        raise ValueError("source_commit must be a lowercase 40-character Git SHA")
    if not RELEASE_TAG.fullmatch(release_tag):
        raise ValueError("release_tag must be a v-prefixed release tag")
    if not package_version:
        raise ValueError("package_version is required")
    if not created_at.endswith("Z"):
        raise ValueError("created_at must be a UTC timestamp ending in Z")

    return {
        "schema": "hrl.image-release/v1",
        "image": image,
        "digest": digest,
        "image_ref": f"{image}@{digest}",
        "source_repository": "lucy-dwr/hrl-restoration-data-pipeline",
        "source_commit": source_commit,
        "release_tag": release_tag,
        "package_version": package_version,
        "created_at": created_at,
        "worker_runtime_contract": WORKER_RUNTIME_CONTRACT,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--digest", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--package-version", required=True)
    parser.add_argument("--created-at", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    args.output.write_text(
        json.dumps(
            image_release(
                image=args.image,
                digest=args.digest,
                source_commit=args.source_commit,
                release_tag=args.release_tag,
                package_version=args.package_version,
                created_at=args.created_at,
            ),
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
