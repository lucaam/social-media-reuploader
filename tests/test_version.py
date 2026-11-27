"""src.__version__: the release version built into the image, else a dev build."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile"


def imported_version(value: str | None) -> str:
    env = {k: v for k, v in os.environ.items() if k != "SMR_VERSION"}
    if value is not None:
        env["SMR_VERSION"] = value
    result = subprocess.run(
        [sys.executable, "-c", "import src; print(src.__version__)"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return result.stdout.strip()


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1.2.3", "1.2.3"), (" 0.9.0\n", "0.9.0"), ("", "0.0.0-dev"), (None, "0.0.0-dev")],
)
def test_version_comes_from_the_build(value, expected):
    assert imported_version(value) == expected


def test_the_dockerfile_passes_the_release_version_to_the_code():
    lines = [
        line.strip() for line in DOCKERFILE.read_text(encoding="utf-8").splitlines()
    ]

    assert "ARG VERSION=0.0.0-dev" in lines
    assert "ENV SMR_VERSION=$VERSION" in lines
    # After the expensive layers: a new version keeps their build cache.
    assert lines.index("ARG VERSION=0.0.0-dev") > max(
        i for i, line in enumerate(lines) if line.startswith("RUN ")
    )
