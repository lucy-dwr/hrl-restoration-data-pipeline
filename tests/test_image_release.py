import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "write_image_release.py"
SPEC = importlib.util.spec_from_file_location("write_image_release", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
image_release = MODULE.image_release


def test_image_release_writes_digest_pinned_reference():
    digest = "sha256:" + "a" * 64
    result = image_release(
        image="example.azurecr.io/hrl-validation-worker",
        digest=digest,
        source_commit="b" * 40,
        release_tag="v0.2.0",
        package_version="0.2.0",
        created_at="2026-08-24T12:00:00Z",
    )

    assert result == {
        "schema": "hrl.image-release/v1",
        "image": "example.azurecr.io/hrl-validation-worker",
        "digest": digest,
        "image_ref": f"example.azurecr.io/hrl-validation-worker@{digest}",
        "source_repository": "lucy-dwr/hrl-restoration-data-pipeline",
        "source_commit": "b" * 40,
        "release_tag": "v0.2.0",
        "package_version": "0.2.0",
        "created_at": "2026-08-24T12:00:00Z",
    }


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("digest", "sha256:" + "A" * 64, "lowercase sha256"),
        ("source_commit", "b" * 39, "40-character Git SHA"),
        ("release_tag", "main", "v-prefixed release tag"),
        ("created_at", "2026-08-24T12:00:00+00:00", "ending in Z"),
    ],
)
def test_image_release_rejects_non_immutable_or_untraceable_values(field, value, message):
    values = {
        "image": "example.azurecr.io/hrl-validation-worker",
        "digest": "sha256:" + "a" * 64,
        "source_commit": "b" * 40,
        "release_tag": "v0.2.0",
        "package_version": "0.2.0",
        "created_at": "2026-08-24T12:00:00Z",
    }
    values[field] = value

    with pytest.raises(ValueError, match=message):
        image_release(**values)
