"""CPU budget of this process: the CPUs it may run on, capped by the cgroup quota.

A container sees every core of the node (``os.cpu_count()``) even when its
CPU limit is one core. Tools that size their thread pools from the core
count (ffmpeg's decoders and filters, x264) then start dozens of threads
that exhaust the CFS quota early in every period: the whole container, the
bot's event loop included, is throttled for the rest of it, and every extra
frame thread costs memory. :func:`cpu_budget` is what callers should size
their parallelism from instead.
"""

from __future__ import annotations

import os

CGROUP_ROOT = "/sys/fs/cgroup"
PROC_CGROUP = "/proc/self/cgroup"


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="ascii") as handle:
            return handle.read().strip()
    except (OSError, UnicodeDecodeError):
        return None


def _quota(quota: str | None, period: str | None) -> float | None:
    """CPUs granted by ``quota``/``period`` (µs); ``None`` when unlimited."""
    try:
        q, p = int(quota or ""), int(period or "")
    except ValueError:
        return None  # "max", "-1" (cgroup v1 unlimited) or unreadable
    return q / p if q > 0 and p > 0 else None


def _v2_path(proc_cgroup: str) -> list[str]:
    """Components of this process's cgroup v2 path (``[]`` at the root)."""
    for line in (_read(proc_cgroup) or "").splitlines():
        if line.startswith("0::"):
            return [part for part in line[3:].split("/") if part not in ("", ".", "..")]
    return []


def cgroup_cpu_limit(
    root: str = CGROUP_ROOT, proc_cgroup: str = PROC_CGROUP
) -> float | None:
    """CPU limit (in CPUs) of this process's cgroup; ``None`` when unlimited.

    cgroup v2: the smallest ``cpu.max`` from this process's cgroup up to the
    mount root (inside a container's cgroup namespace the container *is* the
    root). cgroup v1: the CFS quota of the ``cpu`` controller mount, which a
    container sees as its own cgroup.
    """
    if os.path.exists(os.path.join(root, "cgroup.controllers")):
        parts = _v2_path(proc_cgroup)
        limits = []
        for depth in range(len(parts), -1, -1):
            fields = (_read(os.path.join(root, *parts[:depth], "cpu.max")) or "").split()
            limit = _quota(fields[0], fields[1]) if len(fields) == 2 else None
            if limit is not None:
                limits.append(limit)
        return min(limits, default=None)
    for name in ("cpu", "cpu,cpuacct"):
        directory = os.path.join(root, name)
        limit = _quota(
            _read(os.path.join(directory, "cpu.cfs_quota_us")),
            _read(os.path.join(directory, "cpu.cfs_period_us")),
        )
        if limit is not None:
            return limit
    return None


def usable_cpus() -> int:
    """CPUs this process may be scheduled on (affinity mask / cpuset).

    Honours ``PYTHON_CPU_COUNT`` (``-X cpu_count``) like the standard library.
    """
    return os.process_cpu_count() or os.cpu_count() or 1


def cpu_budget() -> float:
    """CPUs this process can actually use: usable CPUs capped by the cgroup limit.

    Read on every call (a few tiny files): a pod's CPU limit can be resized
    in place without a restart.
    """
    cpus = float(usable_cpus())
    limit = cgroup_cpu_limit()
    return min(cpus, limit) if limit is not None else cpus
