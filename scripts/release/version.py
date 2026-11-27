#!/usr/bin/env python3
"""Plan a release from Conventional Commits: the next version and its notes.

The release tag (``vX.Y.Z``) is the only record of a release: no version is
ever committed to the repository. On every push to ``main`` the release
workflow (``.github/workflows/release.yml``) runs ``plan``, which reads the
commits since the newest release tag reachable from the pushed commit. When
they call for a release, the workflow builds that version into the image
(``--build-arg VERSION``) and the chart (``helm package --version
--app-version``) and then creates the tag and the GitHub release with the
notes written here.

What each commit releases (its header: pull requests are squash-merged, so
the header of a commit on ``main`` is the title of its pull request):

    feat                                     minor
    fix, perf, revert, build(deps)           patch
    ``type!:`` or a ``BREAKING CHANGE:`` footer
                                             major, minor while the version
                                             is 0.y.z
    build, chore, ci, docs, refactor, style, test
                                             nothing

The release bumps by the largest of them.

Commands:

    version.py plan                   the next release (a dry run locally)
    version.py check-title "<title>"  what a pull request title releases

Only the standard library: the workflows run it before installing anything.
"""

from __future__ import annotations

import argparse
import enum
import os
import re
import subprocess
import sys
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

#: Commit types of commitlint's config-conventional (commitlint.config.mjs).
TYPES = frozenset(
    {
        "build",
        "chore",
        "ci",
        "docs",
        "feat",
        "fix",
        "perf",
        "refactor",
        "revert",
        "style",
        "test",
    }
)
#: Types that release a patch version (besides `build(deps)`).
PATCH_TYPES = frozenset({"fix", "perf", "revert"})
#: Scope of dependency updates (Renovate: `fix(deps)` for the image's
#: runtime dependencies, `chore(deps)` / `ci(deps)` for the tooling).
DEPS_SCOPE = "deps"
#: First version when the repository has no release tag yet.
INITIAL_VERSION = "0.1.0"

RELEASE_TAG = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
HEADER = re.compile(
    r"^(?P<type>\w+)(?:\((?P<scope>[^()\r\n]*)\))?(?P<bang>!)?: (?P<subject>.*\S)$"
)
#: Title of the pull requests opened by GitHub's "Revert" button.
GITHUB_REVERT = re.compile(r'^Revert "(?P<header>.+)"$')
#: Reference GitHub appends to the title of a squash-merged pull request.
PR_REFERENCE = re.compile(r"\s+\(#(?P<number>\d+)\)$")
BREAKING_FOOTER = re.compile(r"^BREAKING[ -]CHANGE: ?(?P<text>.*)$")
#: Start of a git trailer / Conventional Commits footer (ends a footer text).
FOOTER = re.compile(r"^[\w-]+(?:: | #)")


class ReleaseError(Exception):
    """The release cannot be planned (reported without a traceback)."""


class Bump(enum.IntEnum):
    """Version increment, ordered: the largest one of a release wins."""

    NONE = 0
    PATCH = 1
    MINOR = 2
    MAJOR = 3

    def __str__(self) -> str:
        return self.name.lower()

    @classmethod
    def parse(cls, value: str) -> Bump:
        """``auto`` / ``none`` / ``patch`` / ``minor`` / ``major``."""
        name = value.strip().lower()
        if name in {"", "auto"}:
            return cls.NONE
        try:
            return cls[name.upper()]
        except KeyError:
            raise ReleaseError(f"unknown bump level {value!r}") from None


@dataclass(frozen=True, order=True)
class Version:
    """A release version ``MAJOR.MINOR.PATCH`` (no pre-release, no build)."""

    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, text: str) -> Version:
        found = cls.from_tag(text if text.startswith("v") else f"v{text}")
        if found is None:
            raise ReleaseError(f"{text!r} is not a version like 1.2.3")
        return found

    @classmethod
    def from_tag(cls, tag: str) -> Version | None:
        """The version of a release tag ``vX.Y.Z``; None for other tags."""
        match = RELEASE_TAG.match(tag)
        if match is None:
            return None
        major, minor, patch = (int(part) for part in match.groups())
        return cls(major, minor, patch)

    @property
    def tag(self) -> str:
        return f"v{self}"

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"

    def bumped(self, bump: Bump) -> Version:
        if bump is Bump.MAJOR:
            return Version(self.major + 1, 0, 0)
        if bump is Bump.MINOR:
            return Version(self.major, self.minor + 1, 0)
        if bump is Bump.PATCH:
            return Version(self.major, self.minor, self.patch + 1)
        return self


@dataclass(frozen=True)
class Change:
    """One commit, as Conventional Commits sees it."""

    sha: str
    #: Lower-case type; empty for a commit that is not conventional.
    type: str
    scope: str
    subject: str
    pr: int | None = None
    breaking: bool = False
    #: Texts of the BREAKING CHANGE footers (may be empty).
    notes: tuple[str, ...] = ()

    @property
    def bump(self) -> Bump:
        if self.breaking:
            return Bump.MAJOR
        if self.type == "feat":
            return Bump.MINOR
        if self.type in PATCH_TYPES:
            return Bump.PATCH
        if self.type == "build" and self.scope == DEPS_SCOPE:
            return Bump.PATCH
        return Bump.NONE

    @property
    def section(self) -> str:
        """Key of the release notes section listing this change."""
        if self.breaking:
            return "breaking"
        if self.scope == DEPS_SCOPE and self.type in {"fix", "build"}:
            return "deps"
        if self.type in {"feat", *PATCH_TYPES}:
            return self.type
        return "other"

    @property
    def header(self) -> str:
        """``type(scope): subject`` (no ``!``, no pull request reference)."""
        if not self.type:
            return self.subject
        scope = f"({self.scope})" if self.scope else ""
        return f"{self.type}{scope}: {self.subject}"


#: Release notes sections, in order.
SECTIONS = (
    ("breaking", "Breaking changes"),
    ("feat", "Features"),
    ("fix", "Bug fixes"),
    ("perf", "Performance"),
    ("revert", "Reverts"),
    ("deps", "Dependencies"),
    ("other", "Other changes"),
)


def footer_notes(lines: Iterable[str]) -> tuple[bool, tuple[str, ...]]:
    """Whether the body has BREAKING CHANGE footers, and their texts."""
    found = False
    notes: list[str] = []
    current: list[str] | None = None
    for line in lines:
        match = BREAKING_FOOTER.match(line)
        if match is not None:
            found = True
            if current is not None:
                notes.append(" ".join(current))
            current = [match["text"].strip()]
        elif current is not None:
            if not line.strip() or FOOTER.match(line):
                notes.append(" ".join(current))
                current = None
            else:
                current.append(line.strip())
    if current is not None:
        notes.append(" ".join(current))
    return found, tuple(note.strip() for note in notes if note.strip())


def parse_commit(sha: str, message: str) -> Change:
    """Parse a commit message (header, body and footers)."""
    lines = message.strip().splitlines() or [""]
    header = lines[0].strip()
    breaking_footer, notes = footer_notes(lines[1:])
    pr = None
    reference = PR_REFERENCE.search(header)
    if reference is not None:
        pr = int(reference["number"])
        header = header[: reference.start()]
    revert = GITHUB_REVERT.match(header)
    if revert is not None:
        return Change(sha, "revert", "", revert["header"], pr, breaking_footer, notes)
    match = HEADER.match(header)
    if match is None:
        # Not conventional: listed in the notes, never released by itself
        # (unless it declares a breaking change).
        return Change(sha, "", "", header, pr, breaking_footer, notes)
    return Change(
        sha=sha,
        type=match["type"].lower(),
        scope=(match["scope"] or "").strip(),
        subject=match["subject"].strip(),
        pr=pr,
        breaking=bool(match["bang"]) or breaking_footer,
        notes=notes,
    )


def release_bump(changes: Iterable[Change], previous: Version | None) -> Bump:
    """The bump the changes call for (a breaking change is minor on 0.y.z)."""
    bump = max((change.bump for change in changes), default=Bump.NONE)
    if bump is Bump.MAJOR and previous is not None and previous.major == 0:
        return Bump.MINOR
    return bump


@dataclass(frozen=True)
class Plan:
    """What the release workflow does for one commit."""

    sha: str
    previous: Version | None
    #: The version to publish; None when there is nothing to release.
    version: Version | None
    bump: Bump
    #: Create the tag and the GitHub release (False when republishing).
    new: bool
    #: Highest release: the image tags `latest`, `X.Y` and `X` move to it.
    latest: bool
    changes: tuple[Change, ...] = ()

    @property
    def release(self) -> bool:
        return self.version is not None

    def outputs(self) -> dict[str, str]:
        """``$GITHUB_OUTPUT`` entries (strings, "true" / "false" flags)."""
        flag = {True: "true", False: "false"}
        version = self.version
        return {
            "release": flag[self.release],
            "new": flag[self.new],
            "version": str(version) if version else "",
            "tag": version.tag if version else "",
            "previous": self.previous.tag if self.previous else "",
            "bump": str(self.bump),
            "sha": self.sha,
            # The `sha-<short>` image tag (7 characters, like `git log`).
            "short_sha": self.sha[:7],
            "latest": flag[self.latest],
            # The `X` image tag only exists from 1.0.0 on (0.y.z is unstable).
            "stable": flag[bool(version and version.major >= 1)],
        }


def plan_release(
    changes: Sequence[Change],
    *,
    sha: str,
    previous: Version | None,
    existing: Collection[Version] = (),
    already_tagged: str = "",
    forced: Bump = Bump.NONE,
    initial: Version | None = None,
) -> Plan:
    """The next release after ``previous`` for ``changes``.

    ``existing``: every release tag of the repository (reachable or not);
    ``already_tagged``: a release tag already on ``sha``; ``forced``: the
    minimum bump asked for by hand (literal: a forced major on 0.y.z
    releases 1.0.0).
    """
    bump = max(release_bump(changes, previous), forced)
    if already_tagged:
        if forced:
            raise ReleaseError(
                f"{sha[:12]} is already released as {already_tagged}: "
                "republish that tag instead of cutting a new release"
            )
        bump = Bump.NONE
    if bump is Bump.NONE:
        return Plan(sha, previous, None, bump, False, False, tuple(changes))
    if previous is None:
        version = initial or Version.parse(INITIAL_VERSION)
    else:
        version = previous.bumped(bump)
    if version in existing:
        raise ReleaseError(
            f"the next version {version.tag} already exists as a tag that is "
            f"not an ancestor of {sha[:12]}: create the release by hand"
        )
    latest = all(version > other for other in existing)
    return Plan(sha, previous, version, bump, True, latest, tuple(changes))


def republished_version(tag: str, existing: Collection[Version]) -> Version:
    """The version of ``tag``, which must be an existing release tag."""
    version = Version.from_tag(tag.strip())
    if version is None:
        raise ReleaseError(f"{tag!r} is not a release tag like v1.2.3")
    if version not in existing:
        raise ReleaseError(f"there is no release tag {version.tag}")
    return version


def plan_republish(
    version: Version, *, sha: str, existing: Collection[Version]
) -> Plan:
    """Publish the image and the chart of an existing release again."""
    latest = all(version >= other for other in existing)
    return Plan(sha, None, version, Bump.NONE, False, latest)


# ---------------------------------------------------------------------------
# Release notes
# ---------------------------------------------------------------------------
def _links(change: Change, repo_url: str | None) -> str:
    refs = []
    if change.pr is not None:
        pr = f"#{change.pr}"
        refs.append(f"[{pr}]({repo_url}/pull/{change.pr})" if repo_url else pr)
    if change.sha:
        short = change.sha[:7]
        refs.append(f"[{short}]({repo_url}/commit/{change.sha})" if repo_url else short)
    return f" ({', '.join(refs)})" if refs else ""


def _entry(change: Change, section: str, repo_url: str | None) -> list[str]:
    if section in {"breaking", "other"}:
        text = change.header
    elif section == "deps" or not change.scope:
        text = change.subject
    else:
        text = f"**{change.scope}:** {change.subject}"
    lines = [f"- {text}{_links(change, repo_url)}"]
    lines.extend(f"  {note}" for note in change.notes)
    return lines


def render_notes(
    plan: Plan,
    *,
    repo_url: str | None = None,
    image: str | None = None,
    chart: str | None = None,
) -> str:
    """Markdown release notes: the changes grouped by type, how to install."""
    if plan.version is None:
        raise ReleaseError("nothing to release")
    grouped: dict[str, list[Change]] = {key: [] for key, _ in SECTIONS}
    for change in plan.changes:
        grouped[change.section].append(change)
    lines: list[str] = []
    for key, title in SECTIONS:
        if grouped[key]:
            lines.extend([f"### {title}", ""])
            for change in grouped[key]:
                lines.extend(_entry(change, key, repo_url))
            lines.append("")
    if not plan.changes:
        lines.extend(["No changes since the previous release.", ""])
    version = plan.version
    if image or chart:
        lines.extend(["### Install", "", "```bash"])
        if image:
            lines.append(f"docker pull {image}:{version}")
        if chart:
            lines.append(
                f"helm upgrade --install smr {chart} --version {version} "
                "--set secret.name=<bot-token-secret>"
            )
        lines.extend(["```", ""])
    if plan.previous and repo_url:
        compare = f"{repo_url}/compare/{plan.previous.tag}...{version.tag}"
        lines.extend([f"**Full changelog**: {compare}", ""])
    return "\n".join(lines).rstrip() + "\n"


def render_summary(plan: Plan) -> str:
    """A short Markdown account of the plan (workflow logs and summary)."""
    since = plan.previous.tag if plan.previous else "the first commit"
    counts: dict[str, int] = {}
    for change in plan.changes:
        kind = change.type or "other"
        counts[kind] = counts.get(kind, 0) + 1
    kinds = ", ".join(f"{n} {kind}" for kind, n in sorted(counts.items()))
    lines = [f"- commit: `{plan.sha[:12]}`"]
    if plan.new or not plan.release:
        lines.append(f"- commits since {since}: {len(plan.changes)} ({kinds or '-'})")
    if plan.version is None:
        lines.append("- **no release**: no feat, fix, perf, revert or breaking change")
    elif plan.new:
        lines.append(f"- **release {plan.version.tag}** ({plan.bump} bump)")
    else:
        lines.append(f"- **republish {plan.version.tag}** (no new tag or release)")
    if plan.version is not None:
        lines.append(f"- moves `latest`: {'yes' if plan.latest else 'no'}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Pull request titles
# ---------------------------------------------------------------------------
def check_title(title: str) -> tuple[bool, str]:
    """Whether a pull request title is a valid commit header, and why."""
    header = title.strip()
    if GITHUB_REVERT.match(header):
        return True, "a **patch** release (revert)"
    match = HEADER.match(header)
    if match is None:
        return False, (
            "not a Conventional Commits header `type(scope): description`, "
            "e.g. `fix(worker): retry uploads after a flood wait`"
        )
    kind = match["type"]
    if kind not in TYPES:
        allowed = ", ".join(sorted(TYPES))
        return False, f"unknown type `{kind}` (allowed, lower case: {allowed})"
    change = parse_commit("", header)
    impact = {
        Bump.NONE: "**no release**",
        Bump.PATCH: "a **patch** release",
        Bump.MINOR: "a **minor** release",
        Bump.MAJOR: "a **major** release (minor while the version is 0.y.z)",
    }[change.bump]
    return True, f"{impact} (`{change.header}`)"


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------
class Git:
    """The few git queries the plan needs (``repo``: the working tree)."""

    def __init__(self, repo: Path | None = None) -> None:
        self.repo = repo

    def __call__(self, *args: str) -> str:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=self.repo,
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except subprocess.CalledProcessError as exc:
            detail = exc.stderr.strip() or exc.stdout.strip()
            raise ReleaseError(f"git {' '.join(args)} failed: {detail}") from exc
        return result.stdout

    def resolve(self, ref: str) -> str:
        return self(
            "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"
        ).strip()

    def is_shallow(self) -> bool:
        return self("rev-parse", "--is-shallow-repository").strip() == "true"

    def release_tags(self, *options: str) -> dict[Version, str]:
        """Release tags (``vX.Y.Z``), filtered by `git tag` ``options``."""
        found: dict[Version, str] = {}
        for name in self("tag", "--list", *options, "v*").split():
            version = Version.from_tag(name)
            if version is not None:
                found[version] = name
        return found

    def changes(self, revisions: str) -> list[Change]:
        """The commits of ``revisions`` (newest first, merges left out)."""
        output = self("log", "-z", "--no-merges", "--format=%H%n%B", revisions)
        changes = []
        for record in output.split("\0"):
            sha, _, message = record.strip("\n").partition("\n")
            if sha:
                changes.append(parse_commit(sha, message))
        return changes


def plan_from_git(
    git: Git, *, ref: str = "HEAD", forced: Bump = Bump.NONE, republish: str = ""
) -> Plan:
    if git.is_shallow():
        raise ReleaseError(
            "the clone is shallow: the plan needs every commit and tag "
            "(actions/checkout with fetch-depth: 0)"
        )
    existing = git.release_tags()
    if republish:
        # Validated before git sees it.
        version = republished_version(republish, existing)
        sha = git.resolve(f"refs/tags/{version.tag}")
        return plan_republish(version, sha=sha, existing=existing)
    sha = git.resolve(ref)
    reachable = git.release_tags("--merged", sha)
    previous = max(reachable, default=None)
    revisions = f"{reachable[previous]}..{sha}" if previous else sha
    here = sorted(git.release_tags("--points-at", sha).values())
    return plan_release(
        git.changes(revisions),
        sha=sha,
        previous=previous,
        existing=existing,
        already_tagged=", ".join(here),
        forced=forced,
    )


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------
def default_repo_url() -> str | None:
    """``https://github.com/<owner>/<repo>`` inside GitHub Actions."""
    server = os.environ.get("GITHUB_SERVER_URL")
    repository = os.environ.get("GITHUB_REPOSITORY")
    return f"{server}/{repository}" if server and repository else None


def _append(path: Path | None, text: str) -> None:
    if path is not None:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(text)


def _error(message: str) -> int:
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::error::{message}")
    print(f"error: {message}", file=sys.stderr)
    return 1


def command_plan(args: argparse.Namespace) -> int:
    # Only an empty --republish means "no republish": anything else (even
    # blanks) must be a release tag, or the run stops before any release.
    plan = plan_from_git(
        Git(args.repo),
        ref=args.ref,
        forced=Bump.parse(args.bump),
        republish=args.republish,
    )
    summary = render_summary(plan)
    print(summary)
    _append(args.summary_file, f"### Release plan\n\n{summary}\n")
    outputs = "".join(f"{key}={value}\n" for key, value in plan.outputs().items())
    _append(args.github_output, outputs)
    if plan.release and plan.new:
        notes = render_notes(
            plan, repo_url=args.repo_url, image=args.image, chart=args.chart
        )
        print(notes)
        if args.notes_file is not None:
            args.notes_file.write_text(notes, encoding="utf-8")
    return 0


def command_check_title(args: argparse.Namespace) -> int:
    ok, explanation = check_title(args.title)
    if ok:
        print(f"Once squash-merged, this pull request makes {explanation}.")
        return 0
    return _error(f"pull request title: {explanation}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        epilog="Bump rules and workflow: see the module docstring and RELEASE.md.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    plan = commands.add_parser("plan", help="the next release of a commit")
    plan.add_argument("--repo", type=Path, help="git working tree (default: cwd)")
    plan.add_argument("--ref", default="HEAD", help="commit to release (HEAD)")
    plan.add_argument(
        "--bump",
        default="auto",
        choices=["auto", "patch", "minor", "major"],
        help="minimum bump: release at least this level (auto: from commits)",
    )
    plan.add_argument(
        "--republish",
        default="",
        metavar="TAG",
        help="plan publishing an existing release tag again (no new release)",
    )
    plan.add_argument("--repo-url", default=default_repo_url())
    plan.add_argument("--image", help="image repository named in the notes")
    plan.add_argument("--chart", help="OCI chart reference named in the notes")
    plan.add_argument("--notes-file", type=Path, help="write the release notes")
    plan.add_argument("--github-output", type=Path, help="append key=value lines")
    plan.add_argument("--summary-file", type=Path, help="append a Markdown summary")
    plan.set_defaults(handler=command_plan)

    title = commands.add_parser("check-title", help="lint a pull request title")
    title.add_argument("title")
    title.set_defaults(handler=command_check_title)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except ReleaseError as exc:
        return _error(str(exc))


if __name__ == "__main__":
    sys.exit(main())
