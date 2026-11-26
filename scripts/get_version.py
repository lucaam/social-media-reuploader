#!/usr/bin/env python3
"""Print the current version: the latest git tag, else src/__init__.py."""

import os
import re
import subprocess


def version_from_init() -> str | None:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    init_py = os.path.join(here, "src", "__init__.py")
    if not os.path.exists(init_py):
        return None
    with open(init_py, encoding="utf-8") as fh:
        text = fh.read()
    m = re.search(r"__version__\s*=\s*['\"]([^'\"]+)['\"]", text)
    return m.group(1) if m else None


def get_latest_tag() -> str:
    try:
        tag = (
            subprocess.check_output(
                ["git", "describe", "--tags", "--abbrev=0"], stderr=subprocess.DEVNULL
            )
            .decode()
            .strip()
        )
        return tag.lstrip("v")
    except (subprocess.CalledProcessError, OSError):
        return version_from_init() or "0.0.0"


if __name__ == "__main__":
    print(get_latest_tag())
