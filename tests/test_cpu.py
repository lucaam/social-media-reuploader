"""CPU budget: cgroup v1/v2 CPU limits capped against the usable CPUs."""

from __future__ import annotations

import pytest

from src import cpu


def _v2(tmp_path, own: str, limits: dict[str, str]):
    root = tmp_path / "cgroup"
    root.mkdir()
    (root / "cgroup.controllers").write_text("cpu memory pids\n")
    for relative, value in limits.items():
        directory = root / relative if relative else root
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "cpu.max").write_text(value + "\n")
    proc = tmp_path / "proc-cgroup"
    proc.write_text(f"0::{own}\n")
    return str(root), str(proc)


def test_v2_container_namespace_reads_its_own_root(tmp_path):
    # inside a cgroup namespace the container is "/" and owns cpu.max
    root, proc = _v2(tmp_path, "/", {"": "150000 100000"})
    assert cpu.cgroup_cpu_limit(root, proc) == 1.5


def test_v2_unlimited(tmp_path):
    root, proc = _v2(tmp_path, "/", {"": "max 100000"})
    assert cpu.cgroup_cpu_limit(root, proc) is None


def test_v2_smallest_limit_on_the_path_to_the_root_wins(tmp_path):
    root, proc = _v2(
        tmp_path,
        "/kubepods.slice/pod/ctr",
        {
            "kubepods.slice": "max 100000",
            "kubepods.slice/pod": "50000 100000",
            "kubepods.slice/pod/ctr": "200000 100000",
        },
    )
    assert cpu.cgroup_cpu_limit(root, proc) == 0.5


def test_v2_missing_or_hostile_paths_fall_back_to_the_root(tmp_path):
    # a process outside the namespace root sees "/../.."; never leave the mount
    root, proc = _v2(tmp_path, "/../../escape/me", {"": "300000 100000"})
    (tmp_path / "escape").mkdir()
    (tmp_path / "escape" / "cpu.max").write_text("10000 100000\n")
    assert cpu.cgroup_cpu_limit(root, proc) == 3.0


@pytest.mark.parametrize(
    "content", ["", "garbage", "100000", "0 100000", "-5 100000", "1 0", "a b"]
)
def test_v2_unreadable_values_mean_no_limit(tmp_path, content):
    root, proc = _v2(tmp_path, "/", {"": content})
    assert cpu.cgroup_cpu_limit(root, proc) is None


def test_v2_without_proc_entry(tmp_path):
    root, _ = _v2(tmp_path, "/", {"": "100000 100000"})
    assert cpu.cgroup_cpu_limit(root, str(tmp_path / "missing")) == 1.0


@pytest.mark.parametrize("controller", ["cpu", "cpu,cpuacct"])
def test_v1_cfs_quota(tmp_path, controller):
    directory = tmp_path / controller
    directory.mkdir()
    (directory / "cpu.cfs_quota_us").write_text("250000\n")
    (directory / "cpu.cfs_period_us").write_text("100000\n")
    assert cpu.cgroup_cpu_limit(str(tmp_path), str(tmp_path / "none")) == 2.5


def test_v1_unlimited_and_no_cgroup_at_all(tmp_path):
    directory = tmp_path / "cpu"
    directory.mkdir()
    (directory / "cpu.cfs_quota_us").write_text("-1\n")
    (directory / "cpu.cfs_period_us").write_text("100000\n")
    assert cpu.cgroup_cpu_limit(str(tmp_path), str(tmp_path / "none")) is None
    empty = tmp_path / "empty"
    empty.mkdir()
    assert cpu.cgroup_cpu_limit(str(empty), str(tmp_path / "none")) is None


def test_budget_is_the_cgroup_limit_capped_by_the_usable_cpus(monkeypatch):
    monkeypatch.setattr(cpu, "usable_cpus", lambda: 8)
    monkeypatch.setattr(cpu, "cgroup_cpu_limit", lambda: 1.5)
    assert cpu.cpu_budget() == 1.5
    monkeypatch.setattr(cpu, "cgroup_cpu_limit", lambda: 32.0)
    assert cpu.cpu_budget() == 8.0  # a quota above the cpuset cannot be used
    monkeypatch.setattr(cpu, "cgroup_cpu_limit", lambda: None)
    assert cpu.cpu_budget() == 8.0


def test_usable_cpus_follows_python_cpu_count(monkeypatch):
    monkeypatch.setattr(cpu.os, "process_cpu_count", lambda: 3)
    assert cpu.usable_cpus() == 3
    monkeypatch.setattr(cpu.os, "process_cpu_count", lambda: None)
    monkeypatch.setattr(cpu.os, "cpu_count", lambda: None)
    assert cpu.usable_cpus() == 1


def test_real_system_budget_is_positive():
    assert cpu.cpu_budget() > 0
    assert cpu.usable_cpus() >= 1
