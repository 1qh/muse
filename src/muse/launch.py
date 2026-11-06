"""Every heavy tool run, MuseScore or the optical cross-check, passes through 1 gate, so 1 build process never runs more at once than its share of the cores."""
from __future__ import annotations

import os
import pathlib
import subprocess
import threading

SLOTS_VARIABLE = "MUSE_LAUNCH_SLOTS"
LAUNCHES = {"count": 0}


def slots() -> int:
    """This process's share of simultaneous launches: what the build assigned it, else every core."""
    given = os.environ.get(SLOTS_VARIABLE)
    return max(1, int(given)) if given else max(1, os.cpu_count() or 1)


GATE = threading.BoundedSemaphore(slots())


def heavy(argv: list[str], timeout: float, check: bool = False, cwd: pathlib.Path | None = None) -> subprocess.CompletedProcess[str]:
    """1 run of a heavy tool, MuseScore or the optical cross-check, waiting for a free slot first; the timeout counts only the run, never the wait; with check, a failed run raises."""
    with GATE:
        LAUNCHES["count"] += 1
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, check=check, cwd=cwd)


def mscore(args: list[str], timeout: float, command: str = "mscore", check: bool = False) -> subprocess.CompletedProcess[str]:
    """1 MuseScore launch through the shared slots."""
    return heavy([command, *args], timeout, check)
