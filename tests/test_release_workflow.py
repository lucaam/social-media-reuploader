""".github/workflows/release.yml: where it publishes and the guard before it.

The guard is a bash step copied at the top of every job that pushes: these
tests keep the copies equal and run that very script against a fake `gh`.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
BASH = shutil.which("bash")
GUARD = "No release in the way"
REPOSITORY = "owner/repo"


def jobs_of(name):
    workflow = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    return workflow["jobs"]


def env_of(name):
    workflow = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    return workflow["env"]


RELEASE = jobs_of("release.yml")
CI = jobs_of("ci.yml")


def scripts(job):
    return [step["run"] for step in job["steps"] if "run" in step]


def step(job, step_id):
    return next(s for s in job["steps"] if s.get("id") == step_id)


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------
def test_every_job_that_pushes_starts_with_the_same_guard():
    pushing = {
        name: job
        for name, job in RELEASE.items()
        if "write"
        in {job.get("permissions", {}).get(p) for p in ("contents", "packages")}
    }
    assert set(pushing) == {"image", "index", "publish"}

    first = [job["steps"][0] for job in pushing.values()]
    assert [s["name"] for s in first] == [GUARD] * 3
    assert first[0] == first[1] == first[2]
    assert first[0]["env"] == {
        "GH_TOKEN": "${{ github.token }}",
        "TAG": "${{ needs.version.outputs.tag }}",
        "NEW": "${{ needs.version.outputs.new }}",
        "LATEST": "${{ needs.version.outputs.latest }}",
    }


def test_only_the_last_job_makes_anything_public():
    publish = "\n".join(scripts(RELEASE["publish"]))
    assert "docker buildx imagetools create" in publish
    assert "helm push" in publish
    assert "gh release create" in publish
    assert RELEASE["publish"]["needs"] == ["version", "index", "chart"]

    for name, job in RELEASE.items():
        if name in {"publish", "gate"}:
            continue
        for script in scripts(job):
            assert "helm push" not in script, name
            assert "gh release create" not in script, name
            for line in script.splitlines():
                # Elsewhere imagetools pushes by digest only.
                if "imagetools create" in line and "--dry-run" not in line:
                    assert '--tag "$IMAGE@$digest"' in line, name


def test_the_release_image_is_built_natively_without_cache():
    image = RELEASE["image"]
    assert image["runs-on"] == "${{ matrix.runner }}"
    assert {
        (leg["platform"], leg["runner"])
        for leg in image["strategy"]["matrix"]["include"]
    } == {
        ("linux/amd64", "ubuntu-24.04"),
        ("linux/arm64", "ubuntu-24.04-arm"),
    }
    build = step(image, "build")["with"]
    assert build["no-cache"] is True
    assert not {"cache-from", "cache-to"} & set(build)
    buildx = next(s for s in image["steps"] if s["name"] == "Set up Docker Buildx")
    assert buildx["with"] == {
        "driver-opts": "image=${{ env.BUILDKIT_IMAGE }}",
        "cache-binary": False,
    }
    for job in RELEASE.values():
        for s in job.get("steps", []):
            assert "setup-qemu-action" not in s.get("uses", "")


def test_the_pull_request_dry_run_builds_like_the_release():
    release = step(RELEASE["image"], "build")["with"]
    dry_run = step(CI["release-dry-run-image"], "build")["with"]
    differ = {"labels", "annotations", "build-args"}
    assert {k: v for k, v in release.items() if k not in differ} == {
        k: v for k, v in dry_run.items() if k not in differ
    }
    assert {
        (leg["platform"], leg["runner"])
        for leg in CI["release-dry-run-image"]["strategy"]["matrix"]["include"]
    } == {("linux/amd64", "ubuntu-24.04"), ("linux/arm64", "ubuntu-24.04-arm")}
    # The same pinned build tools.
    for name in ("BUILDKIT_IMAGE", "SBOM_GENERATOR"):
        assert env_of("ci.yml")[name] == env_of("release.yml")[name]
        assert "@sha256:" in env_of("release.yml")[name]


# ---------------------------------------------------------------------------
# The guard, run
# ---------------------------------------------------------------------------
FAKE_GH = """#!/usr/bin/env bash
# gh api --paginate repos/<repository>/git/matching-refs/tags/v --jq .[].ref
expected="api --paginate repos/$GITHUB_REPOSITORY/git/matching-refs/tags/v --jq .[].ref"
if [ "$*" != "$expected" ]; then
  echo "unexpected gh call: $*" >&2
  exit 2
fi
if [ -n "${FAKE_GH_FAIL:-}" ]; then
  echo "gh: HTTP 502" >&2
  exit 1
fi
printf '%s' "${FAKE_REFS:-}"
"""


@pytest.fixture
def guard(tmp_path):
    """Run the guard: guard(tag, new=, latest=, tags=[...], api_fails=)."""
    if BASH is None:
        pytest.skip("bash is not installed")
    script = RELEASE["publish"]["steps"][0]["run"]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "gh"
    fake.write_text(FAKE_GH, encoding="utf-8")
    fake.chmod(0o755)

    def run(tag, *, new=True, latest=True, tags=(), api_fails=False):
        env = {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "GITHUB_REPOSITORY": REPOSITORY,
            "TAG": tag,
            "NEW": "true" if new else "false",
            "LATEST": "true" if latest else "false",
            "FAKE_REFS": "".join(f"refs/tags/{name}\n" for name in tags),
        }
        if api_fails:
            env["FAKE_GH_FAIL"] = "1"
        # The shell of GitHub Actions for `shell: bash`.
        return subprocess.run(
            [BASH, "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    return run


@pytest.mark.parametrize(
    ("tag", "tags"),
    [
        ("v0.1.0", []),
        ("v0.9.0", ["v0.8.1", "v0.8.0", "v0.7.10"]),
        # Version order, not text order.
        ("v0.10.0", ["v0.9.9", "v0.9.10", "v0.2.0"]),
        # Not release tags: they never stop a release.
        ("v1.0.0", ["v0.9.0", "v2.0.0-rc.1", "v01.0.0", "v3.0", "v3.0.0.1", "vnext"]),
    ],
)
def test_the_guard_lets_the_next_release_through(guard, tag, tags):
    result = guard(tag, tags=tags)

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"No release tag in the way of {tag}" in result.stdout


def test_the_guard_stops_a_release_that_exists(guard):
    result = guard("v0.9.0", tags=["v0.8.1", "v0.9.0"])

    assert result.returncode == 1
    assert "::error::v0.9.0 exists already (released by another run)" in result.stdout


@pytest.mark.parametrize("latest", [True, False])
def test_the_guard_stops_a_release_below_a_newer_one(guard, latest):
    # "Re-run failed jobs" of an old run, after v0.10.0 was released.
    result = guard("v0.9.1", latest=latest, tags=["v0.9.0", "v0.10.0"])

    assert result.returncode == 1
    assert "::error::v0.10.0 is a higher release than v0.9.1" in result.stdout


def test_the_guard_of_a_republished_release(guard):
    tags = ["v0.8.0", "v0.8.1", "v0.9.0"]

    # An older release that leaves `latest` alone, and the newest one.
    assert guard("v0.8.1", new=False, latest=False, tags=tags).returncode == 0
    assert guard("v0.9.0", new=False, latest=True, tags=tags).returncode == 0
    # Planned to move `latest`, but v0.9.0 came out in the meantime.
    moved = guard("v0.8.1", new=False, latest=True, tags=tags)
    assert moved.returncode == 1
    assert "v0.9.0 is a higher release than v0.8.1" in moved.stdout
    # The tag disappeared.
    gone = guard("v0.7.0", new=False, latest=False, tags=tags)
    assert gone.returncode == 1
    assert "::error::v0.7.0 is no longer a release tag" in gone.stdout


def test_the_guard_fails_closed(guard):
    failed = guard("v0.9.0", api_fails=True)
    assert failed.returncode == 1
    assert f"::error::cannot read the tags of {REPOSITORY}" in failed.stdout

    for tag in ("", "0.9.0", "v0.9", "latest"):
        result = guard(tag)
        assert result.returncode == 1
        assert "::error::no release tag in the plan" in result.stdout


def test_the_dry_run_has_one_stable_check_that_can_be_required():
    result = CI["release-dry-run-result"]
    assert result["name"] == "Release dry run"  # no matrix: always reported
    assert set(result["needs"]) == {"release-dry-run", "release-dry-run-image"}
    assert "!cancelled()" in result["if"]  # runs (and passes) when they skip
    assert result["permissions"] == {}
    [script] = scripts(result)
    assert "success|skipped" in script and "exit 1" in script


def test_the_dry_run_plans_on_the_current_main():
    # A re-run reuses the old merge commit: main is added as a second parent
    # so a release published since then is in the history.
    plan = step(CI["release-dry-run"], "plan")
    assert "-p HEAD -p origin/main" in plan["run"]
    assert '--ref "$ref"' in plan["run"]
    assert {"GIT_AUTHOR_NAME", "GIT_COMMITTER_EMAIL"} <= set(plan["env"])
