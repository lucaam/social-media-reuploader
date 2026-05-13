#!/usr/bin/env python3
"""Generate CHANGELOG.md from git tags and Conventional Commits.

Usage: run from repo root. Prints new changelog to stdout.
"""

import os
import re
import subprocess


def run(cmd):
    return subprocess.check_output(cmd, text=True).strip()


def get_origin_owner_repo():
    try:
        origin = run(["git", "config", "--get", "remote.origin.url"])
    except Exception:
        return None
    m = None
    if origin.startswith("git@"):
        m = re.match(r"git@[^:]+:([^/]+)/(.+?)\.git$", origin)
    else:
        m = re.match(r"https?://[^/]+/([^/]+)/(.+?)(?:\.git)?$", origin)
    if not m:
        return None
    owner = m.group(1)
    repo = m.group(2)
    return f"{owner}/{repo}"


def list_tags():
    out = run(["git", "tag", "--list", "--sort=-v:refname"])  # newest first
    raw_tags = [t for t in out.splitlines() if t.strip()]
    # Deduplicate tags that differ only by a leading 'v' (keep first occurrence)
    seen = set()
    tags = []
    for t in raw_tags:
        norm = t[1:] if t.startswith("v") else t
        if norm in seen:
            continue
        seen.add(norm)
        tags.append(t)
    return tags


def initial_commit():
    return run(["git", "rev-list", "--max-parents=0", "HEAD"])[:7]


def commits_in_range(frm, to):
    if frm:
        rng = f"{frm}..{to}"
    else:
        rng = to
    try:
        out = run(
            [
                "git",
                "log",
                "--pretty=format:%H\x01%ad\x01%s",
                "--date=short",
                rng,
            ]
        )
    except subprocess.CalledProcessError:
        return []
    if not out:
        return []
    commits = []
    for line in out.splitlines():
        try:
            sha, date, subj = line.split("\x01", 2)
        except ValueError:
            continue
        commits.append((sha[:7], date, subj))
    return commits


TYPE_MAP = [
    ("feat", "Features"),
    ("fix", "Bug Fixes"),
    ("perf", "Performance Improvements"),
    ("docs", "Documentation"),
    ("chore", "Chores"),
    ("refactor", "Refactors"),
    ("test", "Tests"),
    ("ci", "CI"),
    ("build", "Build"),
]


def group_commits(commits):
    groups = {}
    others = []
    pat = re.compile(r"^(?P<type>[a-zA-Z]+)(\([^)]+\))?(!)?:\s*(?P<desc>.+)")
    for sha, date, subj in commits:
        m = pat.match(subj)
        if m:
            t = m.group("type").lower()
            desc = m.group("desc").strip()
            groups.setdefault(t, []).append((sha, desc))
        else:
            others.append((sha, subj))
    return groups, others


def build_changelog(unreleased_block, tags, owner_repo):
    lines = []
    lines.append("# Changelog\n")
    lines.append("All notable changes to this project are documented in this file.\n")
    lines.append("## Unreleased\n")
    if unreleased_block:
        lines.extend([line.rstrip() for line in unreleased_block])
        lines.append("")
    else:
        lines.append("- No unreleased changes recorded.\n")

    if not tags:
        return "\n".join(lines)

    init = initial_commit()
    for i, tag in enumerate(tags):
        prev_tag = tags[i + 1] if i + 1 < len(tags) else None
        frm = prev_tag if prev_tag else init
        commits = commits_in_range(frm, tag)
        if not commits:
            # No commits — keep note
            try:
                date = run(["git", "show", "-s", "--format=%ad", "--date=short", tag])
            except Exception:
                date = ""
            lines.append(f"## [{tag}] ({date})\n")
            lines.append("- No user-facing commits found for this release.\n")
            lines.append("")
            continue

        groups, others = group_commits(commits)
        try:
            date = run(["git", "show", "-s", "--format=%ad", "--date=short", tag])
        except Exception:
            date = ""

        compare_from = prev_tag if prev_tag else init
        compare_link = None
        if owner_repo:
            compare_link = (
                f"https://github.com/{owner_repo}/compare/{compare_from}...{tag}"
            )

        header = f"## [{tag}]"
        if compare_link:
            header += f"({compare_link})"
        header += f" ({date})\n"
        lines.append(header)

        for tkey, tname in TYPE_MAP:
            items = groups.get(tkey, [])
            if items:
                lines.append(f"### {tname}\n")
                for sha, desc in items:
                    lines.append(f"* {desc} [{sha}]\n")
                lines.append("")

        if others:
            lines.append("### Other changes\n")
            for sha, subj in others:
                lines.append(f"* {subj} [{sha}]\n")
            lines.append("")

    return "\n".join(lines)


def extract_unreleased():
    path = "CHANGELOG.md"
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = f.read().splitlines()
    try:
        idx = next(
            i
            for i, line in enumerate(data)
            if line.strip().lower().startswith("## unreleased")
        )
    except StopIteration:
        return []
    block = []
    for line in data[idx + 1 :]:
        if line.startswith("## "):
            break
        # ignore stray top-level headings accidentally placed inside Unreleased
        if line.startswith("# "):
            continue
        block.append(line)
    return block


def main():
    owner_repo = get_origin_owner_repo()
    tags = list_tags()
    unreleased = extract_unreleased()
    content = build_changelog(unreleased, tags, owner_repo)
    print(content)


if __name__ == "__main__":
    main()
