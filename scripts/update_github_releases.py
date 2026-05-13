#!/usr/bin/env python3
"""Update GitHub Releases from CHANGELOG.md using the `gh` CLI.

This script parses `CHANGELOG.md` and updates (or creates) GitHub Releases
matching the tags found in the changelog. By default runs in dry-run mode
and prints actions. Use `--apply` to perform edits (requires `gh` to be
installed and authenticated).

Usage:
  ./scripts/update_github_releases.py        # dry-run
  ./scripts/update_github_releases.py --apply
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import tempfile
from typing import Dict


def gh_installed() -> bool:
    from shutil import which

    return which("gh") is not None


def parse_changelog(path: str = "CHANGELOG.md") -> Dict[str, str]:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        lines = fh.read().splitlines()

    sections: Dict[str, list[str]] = {}
    current: str | None = None
    buf: list[str] = []
    header_re = re.compile(r"^## \[(?P<tag>[^\]]+)\]")

    for line in lines:
        m = header_re.match(line)
        if m:
            if current:
                sections[current] = buf
            current = m.group("tag")
            buf = []
            continue
        if current:
            buf.append(line)

    if current:
        sections[current] = buf

    # Remove Unreleased if present
    sections.pop("Unreleased", None)
    return {k: "\n".join(v).strip() for k, v in sections.items()}


def gh_release_exists(tag: str) -> bool:
    try:
        subprocess.run(
            ["gh", "release", "view", tag],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except subprocess.CalledProcessError:
        return False


def update_or_create_release(tag: str, notes: str, apply: bool) -> None:
    # write notes to temp file
    with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as tf:
        tf.write(notes + "\n")
        tmpname = tf.name

    try:
        if gh_release_exists(tag):
            print(
                f"[UPDATE] release {tag} -> will set notes ({len(notes.splitlines())} lines)"
            )
            if apply:
                subprocess.run(
                    ["gh", "release", "edit", tag, "--notes-file", tmpname], check=True
                )
        else:
            print(
                f"[CREATE] release {tag} -> will create release with notes ({len(notes.splitlines())} lines)"
            )
            if apply:
                subprocess.run(
                    ["gh", "release", "create", tag, "--notes-file", tmpname],
                    check=True,
                )
    finally:
        try:
            os.unlink(tmpname)
        except Exception:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply", action="store_true", help="Apply changes to GitHub Releases"
    )
    args = parser.parse_args()

    if not gh_installed():
        print("gh CLI not found. Install GitHub CLI and authenticate: `gh auth login`.")
        return 2

    # check auth quickly
    try:
        subprocess.run(
            ["gh", "auth", "status"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        print("gh CLI not authenticated. Run: gh auth login")
        return 3

    sections = parse_changelog()
    if not sections:
        print("No release sections found in CHANGELOG.md")
        return 0

    for tag, notes in sections.items():
        # skip empty notes
        if not notes.strip():
            print(f"Skipping {tag}: no notes")
            continue
        print("---")
        print(f"Tag: {tag}\nPreview:\n{notes[:800]}\n")
        update_or_create_release(tag, notes, args.apply)

    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
