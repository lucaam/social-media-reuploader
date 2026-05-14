#!/usr/bin/env python3
"""
Simple release helper: compute semantic bump from Conventional Commits
and update package `src/__init__.py`, `charts/.../Chart.yaml` (appVersion)
and `charts/.../values.yaml` image tags. Commits changes on a branch
`release/bump-<version>` and pushes it. Prints branch and version lines
on stdout for CI consumption.

This is intentionally lightweight and uses `git` commands available in
GitHub Actions runners.
"""

import os
import re
import subprocess
import sys


def run(cmd):
    return subprocess.run(
        cmd, check=True, capture_output=True, text=True
    ).stdout.strip()


def get_latest_tag():
    try:
        return run(["git", "describe", "--tags", "--abbrev=0"]) or None
    except Exception:
        try:
            tags = run(["git", "tag", "--sort=-creatordate"]) or ""
            return tags.splitlines()[0].strip() if tags else None
        except Exception:
            return None


def get_commits_since(tag):
    if tag:
        rng = f"{tag}..HEAD"
    else:
        rng = "HEAD"
    try:
        out = run(["git", "log", rng, "--pretty=format:%H%x1f%s%x1f%b%x1e"]).strip()
    except subprocess.CalledProcessError:
        return []
    if not out:
        return []
    parts = out.split("\x1e")
    commits = []
    for p in parts:
        if not p.strip():
            continue
        fields = p.split("\x1f")
        if len(fields) < 3:
            continue
        commits.append({"hash": fields[0], "subject": fields[1], "body": fields[2]})
    return commits


def determine_bump(commits):
    major = False
    minor = False
    patch = False
    for c in commits:
        subj = c.get("subject", "")
        body = c.get("body", "")
        # major if explicit BREAKING CHANGE in body or 'type!:' in header
        if re.search(r"^[a-z]+(\([^)]+\))?!:", subj, re.I) or "BREAKING CHANGE" in body:
            major = True
            break
        if re.match(r"^\s*feat(\(|:|!)", subj, re.I):
            minor = True
        else:
            # treat other conventional types as patch-level changes
            patch = True
    if major:
        return "major"
    if minor:
        return "minor"
    if patch:
        return "patch"
    return None


def read_version_from_init(path):
    txt = open(path, "r", encoding="utf-8").read()
    m = re.search(r"__version__\s*=\s*['\"]([^'\"]+)['\"]", txt)
    return m.group(1) if m else None


def write_version_to_init(path, new_version):
    txt = open(path, "r", encoding="utf-8").read()
    new = re.sub(
        r"(__version__\s*=\s*)['\"][^'\"]+['\"]", r"\1'{}'".format(new_version), txt
    )
    open(path, "w", encoding="utf-8").write(new)


def bump_semver(ver, bump):
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)", ver)
    if not m:
        return ver
    major, minor, patch = map(int, m.groups())
    if bump == "major":
        major += 1
        minor = 0
        patch = 0
    elif bump == "minor":
        minor += 1
        patch = 0
    else:
        patch += 1
    return f"{major}.{minor}.{patch}"


def update_chart(chart_path, new_app_version):
    txt = open(chart_path, "r", encoding="utf-8").read()
    # bump chart version patch
    m = re.search(r"^version:\s*([0-9]+\.[0-9]+\.[0-9]+)", txt, re.M)
    if m:
        cv = m.group(1)
        parts = list(map(int, cv.split(".")))
        parts[2] += 1
        new_chart_ver = f"{parts[0]}.{parts[1]}.{parts[2]}"
        txt = re.sub(
            r"^version:\s*[0-9]+\.[0-9]+\.[0-9]+",
            f"version: {new_chart_ver}",
            txt,
            flags=re.M,
        )
    else:
        new_chart_ver = None
    # update appVersion
    if re.search(r"^appVersion:\s*['\"]?[^'\"\n]+['\"]?", txt, re.M):
        txt = re.sub(
            r"^appVersion:\s*['\"]?[^'\"\n]+['\"]?",
            f'appVersion: "{new_app_version}"',
            txt,
            flags=re.M,
        )
    open(chart_path, "w", encoding="utf-8").write(txt)
    return new_chart_ver


def update_values(values_path, new_tag):
    txt = open(values_path, "r", encoding="utf-8").read()
    # replace quoted semver tags (simple heuristic)
    txt2 = re.sub(r"tag:\s*\"[0-9]+\.[0-9]+\.[0-9]+\"", f'tag: "{new_tag}"', txt)
    open(values_path, "w", encoding="utf-8").write(txt2)


def git_commit_and_push(branch, files, message):
    run(["git", "checkout", "-b", branch])
    run(["git", "add"] + files)
    run(["git", "commit", "-m", message])
    run(["git", "push", "-u", "origin", branch])


def main():
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    os.chdir(repo_root)

    last_tag = get_latest_tag()
    commits = get_commits_since(last_tag)
    if not commits:
        # nothing to do
        print("")
        print("")
        return 0

    bump = determine_bump(commits)
    if not bump:
        print("")
        print("")
        return 0

    init_path = os.path.join(repo_root, "src", "__init__.py")
    cur_version = read_version_from_init(init_path) or "0.0.0"
    new_version = bump_semver(cur_version, bump)

    # update files
    write_version_to_init(init_path, new_version)

    chart_path = os.path.join(
        repo_root, "charts", "social-media-reuploader", "Chart.yaml"
    )
    values_path = os.path.join(
        repo_root, "charts", "social-media-reuploader", "values.yaml"
    )
    # update_chart returns the new Chart.yaml `version` but we don't need it here
    update_chart(chart_path, new_version)
    update_values(values_path, new_version)

    branch = f"release/bump-{new_version}"
    message = f"chore(release): bump versions to v{new_version}"
    files = [init_path, chart_path, values_path]
    try:
        git_commit_and_push(branch, files, message)
    except Exception:
        print("", end="\n")
        print("", end="\n")
        raise

    # print branch and version for workflow consumer
    print(branch)
    print(new_version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
