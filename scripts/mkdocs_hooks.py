"""MkDocs hooks (mkdocs.yml `hooks:`): reuse README.md sections in the site.

A documentation page may contain the marker

    <!-- readme-section: NAME -->

which is replaced, at build time, with the part of README.md between
``<!-- section:NAME:start -->`` and ``<!-- section:NAME:end -->``. This way the
configuration reference is written once, in README.md.
"""

from __future__ import annotations

import os
import re

MARKER = re.compile(r"<!--\s*readme-section:\s*([a-z0-9-]+)\s*-->")


def _readme(config) -> str:
    root = os.path.dirname(os.path.abspath(config["config_file_path"]))
    with open(os.path.join(root, "README.md"), encoding="utf-8") as fh:
        return fh.read()


def readme_section(readme: str, name: str) -> str:
    start = f"<!-- section:{name}:start -->"
    end = f"<!-- section:{name}:end -->"
    begin = readme.find(start)
    stop = readme.find(end, begin + len(start)) if begin >= 0 else -1
    if begin < 0 or stop < 0:
        raise ValueError(f"README.md has no section {name!r} ({start} ... {end})")
    return readme[begin + len(start) : stop].strip()


def on_page_markdown(markdown: str, page, config, files) -> str:
    if "readme-section:" not in markdown:
        return markdown
    readme = _readme(config)
    return MARKER.sub(lambda match: readme_section(readme, match.group(1)), markdown)
