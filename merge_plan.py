# SPDX-License-Identifier: GPL-3.0-only
"""Choose how large each merge input may be without exhausting memory.

Merges used to downsize every input to a fixed 6,000-pixel long edge. The
default is now full resolution, bounded by an estimate of the float32 working
set against the memory the machine can actually spare. The estimate is
deliberately coarse and conservative; a visible notice tells the user when
inputs were reduced and to what size.
"""
from __future__ import annotations

import ctypes
import math
import os
import platform
import subprocess

from server_localization import T

FLOAT_RGB_BYTES = 12  # three float32 channels per pixel
MEMORY_SHARE = 0.6    # leave room for the app, caches and the OS
MIN_EDGE = 1024
EDGE_STEP = 16


def working_set_multiplier(mode: str, count: int) -> float:
    """How many full-size float frames a merge holds at its peak."""
    count = max(1, int(count))
    if mode == "focus":
        # Inputs stream one at a time: the aligned frame, two five-level
        # pyramids and their running sums, plus alignment scratch.
        return 6.0
    # HDR fuses a full aligned stack; a panorama keeps every warped tile and
    # its canvas accumulators live at once.
    return float(count) + 3.0


def available_memory_bytes() -> int | None:
    """Best-effort free memory: MemAvailable, macOS vm_stat, or Windows API."""
    try:
        if os.path.exists("/proc/meminfo"):
            with open("/proc/meminfo", encoding="utf-8") as stream:
                for line in stream:
                    if line.startswith("MemAvailable:"):
                        return int(line.split()[1]) * 1024
        if platform.system() == "Darwin":
            page = os.sysconf("SC_PAGE_SIZE")
            output = subprocess.run(["vm_stat"], capture_output=True, text=True,
                                    timeout=5, check=False).stdout
            counts = {}
            for line in output.splitlines():
                if ":" in line:
                    key, _, value = line.partition(":")
                    digits = value.strip().rstrip(".")
                    if digits.isdigit():
                        counts[key.strip()] = int(digits)
            pages = (counts.get("Pages free", 0) + counts.get("Pages inactive", 0)
                     + counts.get("Pages speculative", 0))
            if pages:
                return pages * page
            total = os.sysconf("SC_PHYS_PAGES") * page
            return int(total * 0.5)
        if os.name == "nt":
            class MemoryStatus(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            status = MemoryStatus()
            status.dwLength = ctypes.sizeof(MemoryStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.ullAvailPhys)
        pages = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
        return int(pages * 0.5)
    except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
        return None


def estimate_bytes(mode: str, dimensions: list[tuple[int, int]], edge: int | None) -> int:
    """Peak float32 working bytes for these inputs processed at ``edge``."""
    total_pixels = 0.0
    for width, height in dimensions:
        width, height = max(1, int(width)), max(1, int(height))
        scale = 1.0
        if edge and max(width, height) > edge:
            scale = edge / max(width, height)
        total_pixels = max(total_pixels, width * height * scale * scale)
    return int(total_pixels * FLOAT_RGB_BYTES
               * working_set_multiplier(mode, len(dimensions)))


def plan_input_edge(mode: str, dimensions: list[tuple[int, int]], *,
                    available: int | None = None,
                    hard_cap: int | None = None) -> dict:
    """Return the long-edge limit merge inputs should use, with a notice.

    ``edge`` is None for full resolution. ``hard_cap`` is the explicit
    ``LIGHTTABLE_MERGE_MAX_EDGE`` override, which always applies.
    """
    dimensions = [(max(1, int(w)), max(1, int(h))) for w, h in dimensions] or [(1, 1)]
    full_edge = max(max(w, h) for w, h in dimensions)
    available = available_memory_bytes() if available is None else int(available)
    cap = int(hard_cap) if hard_cap else None
    edge: int | None = cap if cap and cap < full_edge else None
    reason = "cap" if edge else None
    if available is not None and available > 0:
        budget = available * MEMORY_SHARE
        needed = estimate_bytes(mode, dimensions, edge)
        if needed > budget:
            current = edge or full_edge
            fitted = current * math.sqrt(budget / needed)
            fitted = max(MIN_EDGE, int(fitted // EDGE_STEP) * EDGE_STEP)
            if fitted < current:
                edge, reason = fitted, "memory"
    limited = edge is not None and edge < full_edge
    notice = None
    if limited and reason == "memory":
        notice = T("Inputs were reduced to {edge} px on the long edge to fit available memory.",
                   edge=f"{edge:,}")
    elif limited:
        notice = T("Inputs were limited to {edge} px on the long edge by the merge size setting.",
                   edge=f"{edge:,}")
    return {
        "count": len(dimensions), "fullEdge": full_edge,
        "edge": edge if limited else None, "effectiveEdge": edge if limited else full_edge,
        "limited": limited, "reason": reason if limited else None,
        "estimatedBytes": estimate_bytes(mode, dimensions, edge if limited else None),
        "availableBytes": available, "notice": notice,
    }
