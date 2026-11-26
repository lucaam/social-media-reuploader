"""Developer scripts: the commit-msg hook and the local release checks."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash")
GIT = shutil.which("git")

pytestmark = pytest.mark.skipif(BASH is None, reason="bash is not installed")


def write_executable(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


# ---------------------------------------------------------------------------
# scripts/commit-msg-fix.sh (pre-commit commit-msg hook)
# ---------------------------------------------------------------------------
def run_commit_msg_hook(cwd: Path, message: str):
    msgfile = cwd / "COMMIT_EDITMSG"
    msgfile.write_text(message, encoding="utf-8")
    result = subprocess.run(
        [BASH, str(ROOT / "scripts" / "commit-msg-fix.sh"), str(msgfile)],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return result, msgfile


def fake_commitlint(cwd: Path, exit_code: int) -> Path:
    record = cwd / "commitlint-args"
    write_executable(
        cwd / "node_modules" / ".bin" / "commitlint",
        f'#!/bin/sh\nprintf "%s\\n" "$@" > "{record}"\nexit {exit_code}\n',
    )
    return record


def test_commit_msg_hook_does_not_block_commits_without_commitlint(tmp_path):
    # README "Sviluppo" installs the hook without `npm ci`.
    result, msgfile = run_commit_msg_hook(tmp_path, "fix: x\n")

    assert result.returncode == 0, result.stderr
    assert "commitlint not installed" in result.stderr
    assert msgfile.read_text(encoding="utf-8") == "fix: x\n"


@pytest.mark.parametrize("exit_code", [0, 1])
def test_commit_msg_hook_runs_commitlint_when_installed(tmp_path, exit_code):
    record = fake_commitlint(tmp_path, exit_code)

    result, msgfile = run_commit_msg_hook(tmp_path, "fix: x\n")

    assert result.returncode == exit_code
    assert record.read_text(encoding="utf-8").split() == ["--edit", str(msgfile)]


def test_commit_msg_hook_shortens_long_headers(tmp_path):
    header = "fix: " + " ".join(["word"] * 40)
    result, msgfile = run_commit_msg_hook(tmp_path, f"{header}\n\nBody line.\n")

    assert result.returncode == 0, result.stderr
    new_header, rest = msgfile.read_text(encoding="utf-8").split("\n", 1)
    assert len(new_header) <= 120
    assert header.startswith(new_header)
    assert "Body line." in rest


# ---------------------------------------------------------------------------
# scripts/prepare_release.sh
# ---------------------------------------------------------------------------
@pytest.fixture
def release_repo(tmp_path):
    """A clean git checkout to run prepare_release.sh in."""
    if GIT is None:
        pytest.skip("git is not installed")
    repo = tmp_path / "repo"
    repo.mkdir()
    # Git exports GIT_DIR, GIT_INDEX_FILE, ... to hooks (pytest runs as a
    # pre-push hook): drop them, or these commands would use the real repo.
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    for args in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "chore: x"]):
        subprocess.run([GIT, *args], cwd=repo, env=env, check=True, timeout=60)
    return repo


def run_prepare_release(repo: Path, tools: list[str], skip: str | None = None):
    """Run the script with only git, cat and the fake ``tools`` on PATH.

    The fake pre-commit records SKIP and, like the real one, fails when a
    hook whose executable is missing is not skipped. pytest is not on PATH,
    so the test suite is not run recursively.
    """
    bin_dir = repo.parent / "bin"
    bin_dir.mkdir()
    for name in ("git", "cat"):
        (bin_dir / name).symlink_to(shutil.which(name))
    record = repo.parent / "pre-commit-skip"
    write_executable(
        bin_dir / "pre-commit",
        "#!/bin/sh\n"
        f'printf "%s" "$SKIP" > "{record}"\n'
        "for pair in helm-lint:helm actionlint:actionlint; do\n"
        '  hook="${pair%%:*}"; tool="${pair#*:}"\n'
        '  case ",$SKIP," in *",$hook,"*) continue ;; esac\n'
        '  command -v "$tool" >/dev/null 2>&1 || {\n'
        '    echo "Executable \\`$tool\\` not found"; exit 1; }\n'
        "done\n",
    )
    for tool in tools:
        write_executable(bin_dir / tool, "#!/bin/sh\nexit 0\n")
    env = {
        "PATH": str(bin_dir),
        "HOME": str(repo.parent),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    if skip is not None:
        env["SKIP"] = skip
    result = subprocess.run(
        [BASH, str(ROOT / "scripts" / "prepare_release.sh")],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    skipped = record.read_text(encoding="utf-8") if record.exists() else None
    return result, skipped


@pytest.mark.parametrize(
    ("tools", "skip", "expected"),
    [
        ([], None, "helm-lint,actionlint"),
        (["helm"], None, "actionlint"),
        (["actionlint"], None, "helm-lint"),
        (["helm", "actionlint"], None, ""),
        ([], "commitlint", "commitlint,helm-lint,actionlint"),
    ],
)
def test_prepare_release_skips_hooks_whose_tool_is_missing(
    release_repo, tools, skip, expected
):
    result, skipped = run_prepare_release(release_repo, tools, skip)

    assert result.returncode == 0, result.stdout + result.stderr
    assert skipped == expected
    assert "pre-commit checks failed" not in result.stderr
    assert "Local checks passed" in result.stdout
