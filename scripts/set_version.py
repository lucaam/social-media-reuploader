#!/usr/bin/env python3
"""Set package version in src/__init__.py from a tag or provided version.

Usage:
  python3 scripts/set_version.py --version v1.2.3 [--commit] [--branch main]

If --commit is provided the script will stage, commit and push the changed file.
"""

import argparse
import os
import re
import subprocess
import sys


def get_version_from_tag(tag: str | None) -> str | None:
    if tag:
        return tag.lstrip("vV")
    try:
        tag = (
            subprocess.check_output(
                ["git", "describe", "--tags", "--abbrev=0"], timeout=10
            )
            .decode()
            .strip()
        )
        return tag.lstrip("vV")
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        FileNotFoundError,
    ):
        return None
    except (KeyboardInterrupt, SystemExit):
        # propagate control exceptions
        raise


def update_init_py(version: str, path: str = "src/__init__.py") -> bool:
    if not os.path.exists(path):
        print(f"Target file {path} not found", file=sys.stderr)
        return False
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    m = re.search(r"__version__\s*=\s*['\"]([^'\"]+)['\"]", text)
    if m:
        old = m.group(1)
        if old == version:
            print(f"No change needed; {path} already at version {version}")
            return False
        new_text = re.sub(
            r"__version__\s*=\s*['\"][^'\"]+['\"]",
            f'__version__ = "{version}"',
            text,
            count=1,
        )
    else:
        old = None
        new_text = text + f'\n__version__ = "{version}"\n'
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(new_text)
    if old:
        print(f"Updated {path}: {old} -> {version}")
    else:
        print(f"Set {path} to {version}")
    return True


def git_commit_and_push(
    files,
    message: str = "chore(release): bump package version",
    remote: str = "origin",
    branch: str | None = None,
) -> bool:
    try:
        subprocess.check_call(
            [
                "git",
                "config",
                "user.email",
                "github-actions[bot]@users.noreply.github.com",
            ]
        )
        subprocess.check_call(["git", "config", "user.name", "github-actions[bot]"])
        subprocess.check_call(["git", "add"] + files, timeout=20)
        # ensure commit messages from automation do not re-trigger CI
        if "[skip ci]" not in message:
            message = f"{message} [skip ci]"
        subprocess.check_call(["git", "commit", "-m", message], timeout=20)
        if branch:
            subprocess.check_call(["git", "push", remote, f"HEAD:{branch}"], timeout=30)
        else:
            subprocess.check_call(["git", "push", remote], timeout=30)
        print("Pushed commit")
        return True
    except subprocess.TimeoutExpired as e:
        print("Git command timed out:", e, file=sys.stderr)
        return False
    except subprocess.CalledProcessError as e:
        print("Git commit/push failed:", e, file=sys.stderr)
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--version", help="Version string or tag (e.g. v1.2.3 or 1.2.3)"
    )
    parser.add_argument(
        "--commit", action="store_true", help="Commit and push the change"
    )
    parser.add_argument(
        "--branch", help="Branch to push to (defaults to current branch)"
    )
    parser.add_argument("--path", default="src/__init__.py", help="Path to __init__.py")
    args = parser.parse_args()

    version = None
    if args.version:
        version = args.version.lstrip("vV")
    else:
        version = get_version_from_tag(None)

    if not version:
        print("Could not determine version from git tags or --version", file=sys.stderr)
        return 2

    changed = update_init_py(version, path=args.path)
    if changed and args.commit:
        ok = git_commit_and_push(
            [args.path],
            message=f"chore(release): set version {version}",
            branch=args.branch,
        )
        if not ok:
            return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
