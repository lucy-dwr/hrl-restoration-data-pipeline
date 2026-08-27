#!/usr/bin/env python3
"""Import one immutable released LinkML artifact into the local snapshot store.

Run this only in a review branch; commit the resulting snapshot and manifest.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen

REPOSITORY = "https://github.com/Healthy-Rivers-and-Landscapes-Science/hrl-restoration-schema"
RAW_BASE = "https://raw.githubusercontent.com/Healthy-Rivers-and-Landscapes-Science/hrl-restoration-schema"
ARTIFACT = "hrl_restoration_project.yaml"


def resolve_tag(tag: str) -> str:
    output = subprocess.check_output(["git", "ls-remote", "--tags", REPOSITORY, f"{tag}^{{}}"], text=True)
    if not output.strip():
        raise SystemExit(f"{tag} is not an annotated immutable release tag")
    return output.split()[0]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("tag", help="Released annotated schema tag, e.g. v1.2.1")
    args = parser.parse_args()
    commit = resolve_tag(args.tag)
    target = Path("schema-snapshots/hrl-restoration-schema") / args.tag
    if target.exists():
        raise SystemExit(f"refusing to overwrite immutable snapshot: {target}")
    payload = urlopen(f"{RAW_BASE}/{commit}/schemas/{ARTIFACT}").read()
    digest = hashlib.sha256(payload).hexdigest()
    target.mkdir(parents=True)
    (target / ARTIFACT).write_bytes(payload)
    manifest = {"upstream_repository": REPOSITORY, "tag": args.tag, "commit_sha": commit,
                "retrieved_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                "artifacts": {ARTIFACT: digest}}
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Imported {args.tag} at {commit}; SHA-256 {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
