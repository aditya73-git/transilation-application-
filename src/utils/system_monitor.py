"""Lightweight Raspberry Pi system telemetry helpers."""
from __future__ import annotations

import logging
import os
from pathlib import Path
import shutil
import subprocess


THERMAL_PATHS = (
    Path("/sys/class/thermal/thermal_zone0/temp"),
    Path("/sys/devices/virtual/thermal/thermal_zone0/temp"),
)
CPU_FREQ_PATHS = (
    Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq"),
    Path("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_cur_freq"),
)
DEVICE_MODEL_PATHS = (
    Path("/proc/device-tree/model"),
    Path("/sys/firmware/devicetree/base/model"),
)


def _read_text(path: Path) -> str | None:
    """Return file contents as stripped text when available."""
    try:
        return path.read_text(errors="ignore").strip("\x00\r\n ")
    except OSError:
        return None


def _read_first_existing(paths: tuple[Path, ...]) -> str | None:
    """Read the first available file from a list of candidate paths."""
    for path in paths:
        value = _read_text(path)
        if value:
            return value
    return None


def _read_temperature_c() -> float | None:
    """Read CPU temperature in Celsius."""
    raw = _read_first_existing(THERMAL_PATHS)
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value / 1000.0 if value > 1000 else value


def _read_cpu_freq_mhz() -> float | None:
    """Read current CPU frequency in MHz."""
    raw = _read_first_existing(CPU_FREQ_PATHS)
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value / 1000.0 if value > 10000 else value


def _read_meminfo() -> tuple[int | None, int | None, int | None]:
    """Read memory totals from /proc/meminfo in MiB."""
    mem_total_kb = None
    mem_available_kb = None
    swap_free_kb = None
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            name, _, remainder = line.partition(":")
            parts = remainder.strip().split()
            if not parts:
                continue
            value_kb = int(parts[0])
            if name == "MemTotal":
                mem_total_kb = value_kb
            elif name == "MemAvailable":
                mem_available_kb = value_kb
            elif name == "SwapFree":
                swap_free_kb = value_kb
    except (OSError, ValueError):
        return None, None, None

    def to_mib(value_kb: int | None) -> int | None:
        return None if value_kb is None else int(value_kb / 1024)

    return to_mib(mem_total_kb), to_mib(mem_available_kb), to_mib(swap_free_kb)


def _read_uptime_hours() -> float | None:
    """Read system uptime in hours."""
    raw = _read_text(Path("/proc/uptime"))
    if raw is None:
        return None
    try:
        seconds = float(raw.split()[0])
    except (IndexError, ValueError):
        return None
    return seconds / 3600.0


def _read_throttled_status() -> str | None:
    """Read Raspberry Pi throttling flags when vcgencmd is available."""
    vcgencmd = shutil.which("vcgencmd")
    if not vcgencmd:
        return None
    try:
        result = subprocess.run(
            [vcgencmd, "get_throttled"],
            capture_output=True,
            text=True,
            check=False,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def collect_system_snapshot() -> dict:
    """Collect a small, log-friendly system telemetry snapshot."""
    mem_total_mib, mem_available_mib, swap_free_mib = _read_meminfo()
    disk = shutil.disk_usage(".")
    load1, load5, load15 = os.getloadavg()
    temperature_c = _read_temperature_c()
    cpu_freq_mhz = _read_cpu_freq_mhz()
    uptime_hours = _read_uptime_hours()

    memory_used_mib = None
    if mem_total_mib is not None and mem_available_mib is not None:
        memory_used_mib = mem_total_mib - mem_available_mib

    return {
        "device_model": _read_first_existing(DEVICE_MODEL_PATHS),
        "temperature_c": temperature_c,
        "cpu_freq_mhz": cpu_freq_mhz,
        "load_1": load1,
        "load_5": load5,
        "load_15": load15,
        "mem_used_mib": memory_used_mib,
        "mem_available_mib": mem_available_mib,
        "mem_total_mib": mem_total_mib,
        "swap_free_mib": swap_free_mib,
        "disk_free_gib": round(disk.free / (1024 ** 3), 2),
        "disk_total_gib": round(disk.total / (1024 ** 3), 2),
        "uptime_hours": uptime_hours,
        "throttled": _read_throttled_status(),
    }


def format_system_snapshot(snapshot: dict) -> str:
    """Render one telemetry snapshot in a compact, readable line."""
    parts = []

    if snapshot.get("device_model"):
        parts.append(f"device={snapshot['device_model']}")
    if snapshot.get("temperature_c") is not None:
        parts.append(f"temp={snapshot['temperature_c']:.1f}C")
    if snapshot.get("cpu_freq_mhz") is not None:
        parts.append(f"cpu_freq={snapshot['cpu_freq_mhz']:.0f}MHz")

    parts.append(
        "load="
        f"{snapshot['load_1']:.2f}/"
        f"{snapshot['load_5']:.2f}/"
        f"{snapshot['load_15']:.2f}"
    )

    if snapshot.get("mem_used_mib") is not None and snapshot.get("mem_total_mib") is not None:
        parts.append(f"mem={snapshot['mem_used_mib']}MiB/{snapshot['mem_total_mib']}MiB")
    if snapshot.get("mem_available_mib") is not None:
        parts.append(f"mem_free={snapshot['mem_available_mib']}MiB")
    if snapshot.get("swap_free_mib") is not None:
        parts.append(f"swap_free={snapshot['swap_free_mib']}MiB")

    parts.append(
        "disk_free="
        f"{snapshot['disk_free_gib']:.2f}GiB/"
        f"{snapshot['disk_total_gib']:.2f}GiB"
    )

    if snapshot.get("uptime_hours") is not None:
        parts.append(f"uptime={snapshot['uptime_hours']:.1f}h")
    if snapshot.get("throttled"):
        parts.append(f"throttled={snapshot['throttled']}")

    return " | ".join(parts)


def log_system_snapshot(logger: logging.Logger, context: str) -> dict:
    """Collect and write a telemetry snapshot to the logs."""
    snapshot = collect_system_snapshot()
    logger.info("Pi telemetry [%s] %s", context, format_system_snapshot(snapshot))
    return snapshot
