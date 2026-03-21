"""Lightweight runtime performance measurement helpers."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import resource
from typing import Tuple
import time


_PROC_STATUS = Path("/proc/self/status")


@dataclass(frozen=True)
class PerfSample:
    """One snapshot of process runtime metrics."""

    wall_time: float
    self_cpu_time: float
    child_cpu_time: float
    current_rss_bytes: int
    self_peak_rss_bytes: int
    child_peak_rss_bytes: int


def _read_current_rss_bytes() -> int:
    """Read the current process RSS from /proc when available."""
    try:
        for line in _PROC_STATUS.read_text().splitlines():
            if line.startswith("VmRSS:"):
                parts = line.split()
                if len(parts) >= 2:
                    return int(parts[1]) * 1024
    except OSError:
        pass
    return 0


def _usage_cpu_seconds(who: int) -> float:
    """Return user+system CPU seconds for one resource usage scope."""
    usage = resource.getrusage(who)
    return usage.ru_utime + usage.ru_stime


def _usage_peak_rss_bytes(who: int) -> int:
    """Return peak RSS in bytes for one resource usage scope."""
    usage = resource.getrusage(who)
    return int(usage.ru_maxrss) * 1024


def take_perf_sample() -> PerfSample:
    """Capture one runtime performance sample."""
    return PerfSample(
        wall_time=time.perf_counter(),
        self_cpu_time=_usage_cpu_seconds(resource.RUSAGE_SELF),
        child_cpu_time=_usage_cpu_seconds(resource.RUSAGE_CHILDREN),
        current_rss_bytes=_read_current_rss_bytes(),
        self_peak_rss_bytes=_usage_peak_rss_bytes(resource.RUSAGE_SELF),
        child_peak_rss_bytes=_usage_peak_rss_bytes(resource.RUSAGE_CHILDREN),
    )


def bytes_to_mib(num_bytes: int) -> float:
    """Convert bytes to MiB."""
    return num_bytes / (1024 * 1024)


def stage_metrics(start: PerfSample, end: PerfSample) -> dict:
    """Compute performance deltas between two snapshots."""
    wall = max(0.0, end.wall_time - start.wall_time)
    self_cpu = max(0.0, end.self_cpu_time - start.self_cpu_time)
    child_cpu = max(0.0, end.child_cpu_time - start.child_cpu_time)
    total_cpu = self_cpu + child_cpu
    avg_cpu_percent = (total_cpu / wall * 100.0) if wall > 0 else 0.0
    return {
        "wall_seconds": wall,
        "self_cpu_seconds": self_cpu,
        "child_cpu_seconds": child_cpu,
        "total_cpu_seconds": total_cpu,
        "avg_cpu_percent": avg_cpu_percent,
        "current_rss_mib": bytes_to_mib(end.current_rss_bytes),
        "self_peak_rss_mib": bytes_to_mib(end.self_peak_rss_bytes),
        "child_peak_rss_mib": bytes_to_mib(end.child_peak_rss_bytes),
    }


def format_stage_metrics(label: str, start: PerfSample, end: PerfSample) -> str:
    """Render a concise human-readable stage summary."""
    metrics = stage_metrics(start, end)
    return (
        f"Perf | {label}: "
        f"wall={metrics['wall_seconds']:.2f}s "
        f"cpu={metrics['total_cpu_seconds']:.2f}s "
        f"avg_cpu={metrics['avg_cpu_percent']:.0f}% "
        f"rss={metrics['current_rss_mib']:.1f}MiB "
        f"peak={metrics['self_peak_rss_mib']:.1f}MiB "
        f"child_peak={metrics['child_peak_rss_mib']:.1f}MiB"
    )


def summarize_aggregate(label: str, total_wall: float, total_cpu: float, runs: int) -> str:
    """Render aggregate totals across multiple repeated runs."""
    avg_wall = (total_wall / runs) if runs else 0.0
    avg_cpu = (total_cpu / runs) if runs else 0.0
    avg_cpu_percent = (total_cpu / total_wall * 100.0) if total_wall > 0 else 0.0
    return (
        f"Perf | {label}: "
        f"runs={runs} "
        f"total_wall={total_wall:.2f}s "
        f"avg_wall={avg_wall:.2f}s "
        f"total_cpu={total_cpu:.2f}s "
        f"avg_cpu={avg_cpu:.2f}s "
        f"avg_cpu_load={avg_cpu_percent:.0f}%"
    )
