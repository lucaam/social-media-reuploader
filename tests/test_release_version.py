"""scripts/release/version.py: next version and release notes from commits."""

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release" / "version.py"
GIT = shutil.which("git")
REPO_URL = "https://github.com/owner/repo"


def load_module():
    spec = importlib.util.spec_from_file_location("_release_version", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    # dataclasses look the module up in sys.modules while building classes.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rv = load_module()
Bump, Version = rv.Bump, rv.Version


def change(message: str, sha: str = "a" * 40):
    return rv.parse_commit(sha, message)


def sha_of(index: int) -> str:
    """A fake commit id whose short form is the 7-digit index."""
    return f"{index:07d}".ljust(40, "0")


def plan(messages, previous="0.8.1", **kwargs):
    changes = [change(m, sha=sha_of(i)) for i, m in enumerate(messages, 1)]
    previous = Version.parse(previous) if previous else None
    existing = kwargs.pop("existing", [previous] if previous else [])
    return rv.plan_release(
        changes, sha="f" * 40, previous=previous, existing=existing, **kwargs
    )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def test_parses_a_squash_merged_pull_request_title():
    parsed = change("fix(worker): keep the queue on restart (#94)\n\nBody.")

    assert (parsed.type, parsed.scope, parsed.subject) == (
        "fix",
        "worker",
        "keep the queue on restart",
    )
    assert parsed.pr == 94
    assert not parsed.breaking


def test_parses_a_header_without_scope_or_pull_request():
    parsed = change("feat: add an album mode")

    assert (parsed.type, parsed.scope, parsed.pr) == ("feat", "", None)
    assert parsed.header == "feat: add an album mode"


@pytest.mark.parametrize(
    "message",
    [
        "feat(api)!: drop the v1 endpoints",
        "chore!: require Python 3.14",
        "feat: new queue\n\nBREAKING CHANGE: the old queue table is dropped",
        "fix: x\n\nBREAKING-CHANGE: renamed a setting",
        "fix: x\n\nSome text.\n\nBREAKING CHANGE:\nCo-authored-by: A <a@b.c>",
    ],
)
def test_detects_breaking_changes(message):
    assert change(message).breaking


def test_keeps_the_text_of_breaking_change_footers():
    parsed = change(
        "feat(chart): rework values (#7)\n\n"
        "BREAKING CHANGE: image.tag defaults to the chart appVersion;\n"
        "set it only to pin another release.\n"
        "Refs: #6\n"
        "Co-authored-by: A <a@example.com>"
    )

    assert parsed.notes == (
        (
            "image.tag defaults to the chart appVersion; set it only to pin "
            "another release."
        ),
    )


@pytest.mark.parametrize(
    "message",
    [
        "fix: mention BREAKING CHANGE: in the middle of a line",
        "docs: explain breaking change: footers",
        "feat: x\n\n* fix!: a squashed commit listed in the body",
    ],
)
def test_ignores_breaking_change_mentions_that_are_not_footers(message):
    assert not change(message).breaking


@pytest.mark.parametrize(
    ("message", "subject"),
    [
        ('Revert "feat: add albums (#12)" (#13)', "feat: add albums (#12)"),
        ("revert: feat: add albums", "feat: add albums"),
    ],
)
def test_parses_reverts(message, subject):
    parsed = change(message)

    assert parsed.type == "revert"
    assert parsed.subject == subject
    assert parsed.bump is Bump.PATCH


@pytest.mark.parametrize(
    "message",
    ["Update README.md", "fix:missing space", "fix(: unbalanced", "WIP", ""],
)
def test_non_conventional_commits_release_nothing(message):
    parsed = change(message)

    assert parsed.type == ""
    assert parsed.bump is Bump.NONE
    assert parsed.section == "other"


# ---------------------------------------------------------------------------
# Bump rules
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("message", "bump"),
    [
        ("feat: x", Bump.MINOR),
        ("feat(bot): x (#3)", Bump.MINOR),
        ("fix: x", Bump.PATCH),
        ("perf(media): x", Bump.PATCH),
        ("revert: x", Bump.PATCH),
        ("fix(deps): update dependency yt-dlp to v2026.12.1", Bump.PATCH),
        ("build(deps): bump aiogram", Bump.PATCH),
        ("build(docker): x", Bump.NONE),
        ("chore(deps): update dependency ruff to v0.16.1", Bump.NONE),
        ("ci(deps): update actions/checkout action to v7.0.2", Bump.NONE),
        ("chore: x", Bump.NONE),
        ("docs: x", Bump.NONE),
        ("ci: x", Bump.NONE),
        ("test: x", Bump.NONE),
        ("style: x", Bump.NONE),
        ("refactor: x", Bump.NONE),
        ("Feat: upper-case types still count", Bump.MINOR),
        ("refactor!: x", Bump.MAJOR),
        ("docs: x\n\nBREAKING CHANGE: y", Bump.MAJOR),
    ],
)
def test_bump_of_each_commit(message, bump):
    assert change(message).bump is bump


@pytest.mark.parametrize(
    ("previous", "messages", "expected"),
    [
        ("0.8.1", ["fix: a"], "0.8.2"),
        ("0.8.1", ["fix: a", "feat: b", "chore: c"], "0.9.0"),
        # 0.y.z: a breaking change bumps the minor version.
        ("0.8.1", ["feat!: a", "fix: b"], "0.9.0"),
        ("1.2.3", ["fix: a"], "1.2.4"),
        ("1.2.3", ["feat: a"], "1.3.0"),
        ("1.2.3", ["fix: a\n\nBREAKING CHANGE: b"], "2.0.0"),
        ("0.9.9", ["fix(deps): a"], "0.9.10"),
    ],
)
def test_next_version(previous, messages, expected):
    result = plan(messages, previous=previous)

    assert result.release and result.new
    assert str(result.version) == expected
    assert result.version.tag == f"v{expected}"


@pytest.mark.parametrize(
    "messages",
    [[], ["chore: a", "docs: b", "ci: c", "test: d", "refactor: e", "style: f"]],
)
def test_no_releasable_commit_means_no_release(messages):
    result = plan(messages)

    assert not result.release
    assert result.version is None
    assert result.outputs()["release"] == "false"
    assert result.outputs()["version"] == ""


def test_the_first_release_starts_at_0_1_0():
    result = plan(["feat: a", "fix: b"], previous=None)

    assert str(result.version) == "0.1.0"
    assert result.previous is None


@pytest.mark.parametrize(
    ("forced", "messages", "expected"),
    [
        (Bump.PATCH, [], "0.8.2"),
        (Bump.PATCH, ["chore: a"], "0.8.2"),
        # The forced level is a minimum: the commits may ask for more.
        (Bump.PATCH, ["feat: a"], "0.9.0"),
        (Bump.MINOR, ["fix: a"], "0.9.0"),
        # Literal: a forced major on 0.y.z releases 1.0.0.
        (Bump.MAJOR, ["fix: a"], "1.0.0"),
    ],
)
def test_forced_bumps(forced, messages, expected):
    assert str(plan(messages, forced=forced).version) == expected


def test_an_already_released_commit_is_not_released_again():
    assert not plan(["fix: a"], already_tagged="v0.8.1").release
    with pytest.raises(rv.ReleaseError, match="already released as v0.8.1"):
        plan(["fix: a"], already_tagged="v0.8.1", forced=Bump.PATCH)


def test_refuses_a_version_that_exists_elsewhere():
    existing = [Version.parse("0.8.1"), Version.parse("0.8.2")]

    with pytest.raises(rv.ReleaseError, match="v0.8.2 already exists"):
        plan(["fix: a"], existing=existing)


def test_latest_only_for_the_highest_release():
    assert plan(["fix: a"]).latest
    existing = [Version.parse("0.8.1"), Version.parse("1.0.0")]
    assert not plan(["fix: a"], existing=existing).latest


def test_outputs_for_the_workflow():
    outputs = plan(["feat: a"]).outputs()

    assert outputs == {
        "release": "true",
        "new": "true",
        "version": "0.9.0",
        "tag": "v0.9.0",
        "previous": "v0.8.1",
        "bump": "minor",
        "sha": "f" * 40,
        "short_sha": "f" * 7,
        "latest": "true",
        "stable": "false",
    }
    assert plan(["feat: a"], previous="1.0.0").outputs()["stable"] == "true"


@pytest.mark.parametrize(
    ("text", "bump"),
    [("auto", Bump.NONE), ("patch", Bump.PATCH), ("MINOR", Bump.MINOR)],
)
def test_bump_levels_from_text(text, bump):
    assert Bump.parse(text) is bump


@pytest.mark.parametrize("text", ["1.2", "v1.2.3-rc.1", "01.2.3", "latest"])
def test_versions_are_plain_semver(text):
    with pytest.raises(rv.ReleaseError):
        Version.parse(text)
    assert Version.from_tag(f"v{text}") is None


def test_versions_sort_numerically():
    tags = ["v0.10.0", "v0.9.1", "v0.9.10", "v1.0.0", "v0.2.2"]

    assert max(Version.from_tag(t) for t in tags) == Version(1, 0, 0)
    assert sorted(Version.from_tag(t) for t in tags)[1:3] == [
        Version(0, 9, 1),
        Version(0, 9, 10),
    ]


def test_republish_plan():
    existing = [Version.parse(v) for v in ("0.9.0", "0.9.1", "0.10.0")]

    old = rv.plan_republish(Version(0, 9, 1), sha="c" * 40, existing=existing)
    assert old.release and not old.new and not old.latest
    assert old.outputs()["version"] == "0.9.1"
    newest = rv.plan_republish(Version(0, 10, 0), sha="c" * 40, existing=existing)
    assert newest.latest
    with pytest.raises(rv.ReleaseError, match="no release tag v0.9.2"):
        rv.republished_version("v0.9.2", existing)
    with pytest.raises(rv.ReleaseError, match="not a release tag"):
        rv.republished_version("main", existing)


# ---------------------------------------------------------------------------
# Release notes
# ---------------------------------------------------------------------------
def test_release_notes_group_the_changes_by_type():
    result = plan(
        [
            "docs: explain the chart (#9)",
            "fix(deps): update dependency yt-dlp to v2026.12.1 (#8)",
            'Revert "feat: albums (#5)" (#7)',
            "perf(media): faster probes (#6)",
            "fix(worker): keep the queue (#4)",
            "feat(gui): live stats (#3)",
            (
                "feat(chart)!: one PVC for bot and GUI (#2)\n\n"
                "BREAKING CHANGE: the PVC is renamed."
            ),
            "Update something by hand",
        ]
    )

    notes = rv.render_notes(result, repo_url=REPO_URL)

    headings = [line for line in notes.splitlines() if line.startswith("### ")]
    assert headings == [
        "### Breaking changes",
        "### Features",
        "### Bug fixes",
        "### Performance",
        "### Reverts",
        "### Dependencies",
        "### Other changes",
    ]
    assert "- feat(chart): one PVC for bot and GUI ([#2](" in notes
    assert "  the PVC is renamed." in notes
    assert (
        "- **gui:** live stats ([#3](https://github.com/owner/repo/pull/3), " in notes
    )
    assert f"- **media:** faster probes ([#6]({REPO_URL}/pull/6), " in notes
    assert f"[0000004]({REPO_URL}/commit/{sha_of(4)})" in notes
    assert "- feat: albums (#5) ([#7](" in notes
    assert "- update dependency yt-dlp to v2026.12.1 ([#8](" in notes
    assert "- docs: explain the chart ([#9](" in notes
    assert "- Update something by hand (" in notes
    assert notes.endswith(f"**Full changelog**: {REPO_URL}/compare/v0.8.1...v0.9.0\n")


def test_release_notes_without_repository_url_and_with_install_commands():
    result = plan(["fix: a (#4)"], forced=Bump.PATCH)

    notes = rv.render_notes(
        result,
        image="ghcr.io/owner/app",
        chart="oci://ghcr.io/owner/charts/app",
    )

    assert "- a (#4, 0000001)" in notes
    assert "docker pull ghcr.io/owner/app:0.8.2" in notes
    assert "oci://ghcr.io/owner/charts/app --version 0.8.2" in notes
    assert "Full changelog" not in notes


def test_release_notes_of_a_forced_release_without_changes():
    notes = rv.render_notes(plan([], forced=Bump.PATCH))

    assert "No changes since the previous release." in notes


# ---------------------------------------------------------------------------
# Pull request titles
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("title", "impact"),
    [
        ("fix(worker): retry uploads", "a **patch** release"),
        ("feat: albums", "a **minor** release"),
        ("feat(api)!: drop v1", "a **major** release"),
        ("docs: typo", "**no release**"),
        ("fix(deps): update dependency yt-dlp to v2026.12.1", "a **patch** release"),
        ('Revert "feat: albums (#5)"', "a **patch** release"),
    ],
)
def test_check_title_accepts_conventional_titles(title, impact):
    ok, explanation = rv.check_title(title)

    assert ok
    assert explanation.startswith(impact)


@pytest.mark.parametrize(
    ("title", "reason"),
    [
        ("Update README.md", "not a Conventional Commits header"),
        ("fix:no space", "not a Conventional Commits header"),
        ("fix: ", "not a Conventional Commits header"),
        ("feature: albums", "unknown type `feature`"),
        ("Fix: albums", "unknown type `Fix`"),
    ],
)
def test_check_title_rejects_other_titles(title, reason):
    ok, explanation = rv.check_title(title)

    assert not ok
    assert reason in explanation


def test_check_title_command(capsys):
    assert rv.main(["check-title", "fix(bot): x"]) == 0
    assert "a **patch** release" in capsys.readouterr().out
    assert rv.main(["check-title", "nope"]) == 1
    assert "error: pull request title" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Against a real git repository
# ---------------------------------------------------------------------------
@pytest.fixture
def repo(tmp_path, monkeypatch):
    """An empty git repository; commit(message) and tag(name) helpers."""
    if GIT is None:
        pytest.skip("git is not installed")
    # Git exports GIT_DIR, GIT_INDEX_FILE, ... to hooks (pytest runs as a
    # pre-push hook): drop them, or git would use the real repository.
    for key in list(os.environ):
        if key.startswith("GIT_"):
            monkeypatch.delenv(key)
    for key, value in {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }.items():
        monkeypatch.setenv(key, value)
    path = tmp_path / "repo"
    path.mkdir()

    def git(*args):
        return subprocess.run(
            [GIT, *args], cwd=path, check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-q", "-b", "main")

    class Repo:
        root = path
        run = staticmethod(git)

        @staticmethod
        def commit(message):
            git("commit", "-q", "--allow-empty", "-m", message)
            return git("rev-parse", "HEAD")

        @staticmethod
        def tag(name, ref="HEAD"):
            git("tag", name, ref)

    return Repo


def test_plan_from_git_reads_the_commits_since_the_latest_reachable_tag(repo):
    repo.commit("feat: first")
    repo.tag("v0.1.0")
    repo.tag("not-a-release")
    repo.tag("v0.1.0-rc.1")
    repo.commit("feat: second")
    repo.tag("v0.2.0")
    repo.commit("fix(worker): x (#12)")
    repo.commit("chore: y")
    head = repo.commit("docs: z\n\nLonger body.\n\nRefs: #3")

    result = rv.plan_from_git(rv.Git(repo.root))

    assert result.previous == Version(0, 2, 0)
    assert str(result.version) == "0.2.1"
    assert result.sha == head
    assert [c.type for c in result.changes] == ["docs", "chore", "fix"]
    assert result.changes[2].pr == 12


def test_plan_from_git_ignores_tags_of_other_branches_but_refuses_collisions(repo):
    repo.commit("feat: first")
    repo.tag("v1.0.0")
    repo.run("checkout", "-q", "-b", "hotfix")
    repo.commit("fix: hotfix")
    repo.tag("v1.0.1")
    repo.tag("v2.0.0")
    repo.run("checkout", "-q", "main")
    repo.commit("feat: on main")

    result = rv.plan_from_git(rv.Git(repo.root))
    assert result.previous == Version(1, 0, 0)
    assert str(result.version) == "1.1.0"
    assert not result.latest  # v2.0.0 exists on another branch

    repo.commit("chore: noise")
    repo.run("reset", "-q", "--hard", "HEAD~2")
    repo.commit("fix: only a fix")
    with pytest.raises(rv.ReleaseError, match="v1.0.1 already exists"):
        rv.plan_from_git(rv.Git(repo.root))


def test_plan_from_git_without_tags_and_with_a_tagged_head(repo):
    repo.commit("chore: init")
    repo.commit("fix: a")

    first = rv.plan_from_git(rv.Git(repo.root))
    assert str(first.version) == "0.1.0"
    assert len(first.changes) == 2

    repo.tag("v0.1.0")
    assert not rv.plan_from_git(rv.Git(repo.root)).release
    with pytest.raises(rv.ReleaseError, match="already released as v0.1.0"):
        rv.plan_from_git(rv.Git(repo.root), forced=Bump.PATCH)


def test_plan_from_git_republishes_an_existing_tag(repo):
    sha = repo.commit("feat: a")
    repo.tag("v0.3.0")
    repo.commit("fix: b")

    result = rv.plan_from_git(rv.Git(repo.root), republish="v0.3.0")

    assert result.sha == sha
    assert (str(result.version), result.new, result.latest) == ("0.3.0", False, True)
    with pytest.raises(rv.ReleaseError, match="no release tag v0.4.0"):
        rv.plan_from_git(rv.Git(repo.root), republish="v0.4.0")


def test_plan_from_git_refuses_shallow_clones(repo, tmp_path):
    repo.commit("feat: a")
    repo.commit("fix: b")
    clone = tmp_path / "clone"
    subprocess.run(
        [GIT, "clone", "-q", "--depth", "1", repo.root.as_uri(), str(clone)],
        check=True,
        capture_output=True,
    )

    with pytest.raises(rv.ReleaseError, match="shallow"):
        rv.plan_from_git(rv.Git(clone))


def test_plan_command_writes_the_workflow_outputs_and_the_notes(repo, tmp_path):
    repo.commit("feat: a")
    repo.tag("v0.4.2")
    repo.commit("feat(gui): b (#5)")
    outputs, notes, summary = (tmp_path / n for n in ("out", "notes.md", "sum"))
    outputs.write_text("earlier=entry\n", encoding="utf-8")

    code = rv.main(
        [
            "plan",
            "--repo",
            str(repo.root),
            "--repo-url",
            REPO_URL,
            "--image",
            "ghcr.io/owner/repo",
            "--github-output",
            str(outputs),
            "--notes-file",
            str(notes),
            "--summary-file",
            str(summary),
        ]
    )

    assert code == 0
    lines = outputs.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "earlier=entry"
    assert "version=0.5.0" in lines
    assert "tag=v0.5.0" in lines
    assert "new=true" in lines
    assert (
        "- **gui:** b ([#5](https://github.com/owner/repo/pull/5)"
        in notes.read_text(encoding="utf-8")
    )
    assert "**release v0.5.0** (minor bump)" in summary.read_text(encoding="utf-8")


def test_plan_command_reports_errors_without_a_traceback(repo, capsys, monkeypatch):
    repo.commit("feat: a")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    code = rv.main(["plan", "--repo", str(repo.root), "--republish", "v9.9.9"])

    assert code == 1
    captured = capsys.readouterr()
    assert "::error::there is no release tag v9.9.9" in captured.out
    assert "Traceback" not in captured.err


def test_a_blank_republish_tag_is_an_error_not_a_new_release(repo, tmp_path):
    # The workflow skips the gate for any non-empty `tag` input: blanks must
    # not turn into an ungated new release.
    repo.commit("feat: a")
    outputs = tmp_path / "out"

    code = rv.main(
        [
            "plan",
            "--repo",
            str(repo.root),
            "--republish",
            " ",
            "--github-output",
            str(outputs),
        ]
    )

    assert code == 1
    assert not outputs.exists()


def test_the_script_runs_standalone(repo):
    repo.commit("fix: a")

    result = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "plan", "--repo", str(repo.root)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "**release v0.1.0**" in result.stdout
