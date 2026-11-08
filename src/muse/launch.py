"""Every heavy tool run, MuseScore or the optical cross-check, passes through 1 gate, so 1 build process never runs more at once than its share of the cores."""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable

import psutil

SLOTS_VARIABLE = "MUSE_LAUNCH_SLOTS"
LAUNCHES = {"count": 0}


def slots() -> int:
    """This process's share of simultaneous launches: what the build assigned it, else every core."""
    given = os.environ.get(SLOTS_VARIABLE)
    return max(1, int(given)) if given else max(1, os.cpu_count() or 1)


GATE = threading.BoundedSemaphore(slots())


FREE_CORE_WAIT = 3600.0  # seconds a launch waits for a whole core to sit idle
IDLE_WINDOW = 1.0  # seconds over which 1 reading measures idle CPU


def idle_percent() -> float:
    """The share of all CPU time idle over the last second, measured, never the load average, which counts waiting threads."""
    return 100.0 - float(psutil.cpu_percent(interval=IDLE_WINDOW))


def free_core(deadline: float = FREE_CORE_WAIT, idle: Callable[[], float] = idle_percent, cores: int | None = None) -> None:
    """Wait until a whole core sits idle: the cores are shared with other work, and a run started on a saturated machine starves past its deadline; each reading itself takes the measuring second."""
    total = cores or os.cpu_count() or 1
    give_up = time.monotonic() + deadline
    while (free := idle() * total / 100.0) < 1.0:
        if time.monotonic() > give_up:
            raise TimeoutError(f"no free core for {deadline:.0f} s: {free:.2f} of {total} cores idle")


HANGS = pathlib.Path(tempfile.gettempdir()) / "muse-hangs"  # native stacks of runs that passed their deadline
SAMPLE_SECONDS = 3


def heavy(argv: list[str], timeout: float, check: bool = False, cwd: pathlib.Path | None = None) -> subprocess.CompletedProcess[str]:
    """1 run of a heavy tool, MuseScore or the optical cross-check, waiting for a free slot first; the timeout counts only the run, never the wait; with check, a failed run raises; a run past its deadline is sampled before it is killed."""
    with GATE:
        free_core()
        LAUNCHES["count"] += 1
        with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, stdin=subprocess.DEVNULL, cwd=cwd) as process:
            try:
                out, err = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired as expired:
                where = sample(process.pid)
                process.kill()
                process.communicate()
                raise subprocess.TimeoutExpired(argv, timeout, output=f"stack sampled to {where}") from expired
        done = subprocess.CompletedProcess(argv, process.returncode, out, err)
        if check:
            done.check_returncode()
        return done


def sample(pid: int) -> pathlib.Path | None:
    """The native stack of a running process, written where a reader can find it after the process is gone; none where the sampler is absent."""
    tool = shutil.which("sample")
    if tool is None:
        return None
    HANGS.mkdir(parents=True, exist_ok=True)
    target = HANGS / f"{pid}-{int(time.time())}.sample"
    subprocess.run([tool, str(pid), str(SAMPLE_SECONDS), "-file", str(target)], capture_output=True, stdin=subprocess.DEVNULL, timeout=SAMPLE_SECONDS * 10, check=False)
    return target


def mscore(args: list[str], timeout: float, command: str = "mscore", check: bool = False) -> subprocess.CompletedProcess[str]:
    """1 MuseScore launch through the shared slots."""
    return heavy([command, *args], timeout, check)
