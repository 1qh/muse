"""Every heavy tool run, MuseScore or the optical cross-check, passes through 1 gate, so 1 build process never runs more at once than its share of the cores."""
from __future__ import annotations

import os
import pathlib
import re
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


LOGS = pathlib.Path.home() / "Library" / "Application Support" / "MuseScore" / "MuseScore4" / "logs"  # MuseScore writes 1 log per launch second and never prunes
LOG_NAME = re.compile(r"MuseScore_(\d{6}_\d{6})\.log")
CONVERSION = re.compile(r"ConverterController::convertFile \| in: (.+?), out: ")


def mscore(args: list[str], timeout: float, command: str = "mscore", check: bool = False) -> subprocess.CompletedProcess[str]:
    """1 MuseScore launch through the shared slots, its own logs removed after it."""
    began = time.time()
    try:
        return heavy([command, *args], timeout, check)
    finally:
        drop_own_logs(roots_of(args), began - 1, time.time() + 1)


def roots_of(args: list[str]) -> list[pathlib.Path]:
    """The folders a launch reads from: each path argument's own, and for a job file, each path it lists."""
    paths = [pathlib.Path(arg) for arg in args if pathlib.Path(arg).exists()]
    for job in [path for path in paths if path.suffix == ".json"]:
        paths += [pathlib.Path(value) for value in re.findall(r'"([^"]+)"', job.read_text(encoding="utf-8")) if value.startswith("/") and pathlib.Path(value).exists()]
    return sorted({path.resolve().parent for path in paths})


def drop_own_logs(roots: list[pathlib.Path], start: float, end: float, logs: pathlib.Path | None = None) -> int:
    """Delete each log named for a second between start and end whose every conversion reads a file under 1 of the roots; a log with no conversion, the owner's own session, stays."""
    folder = logs or LOGS
    if not roots or not folder.is_dir():
        return 0
    dropped = 0
    for log in folder.iterdir():
        named = LOG_NAME.fullmatch(log.name)
        if not named or not start <= time.mktime(time.strptime(named[1], "%y%m%d_%H%M%S")) <= end:
            continue
        reads = CONVERSION.findall(log.read_text(encoding="utf-8", errors="replace"))
        if reads and all(any(pathlib.Path(read).resolve().is_relative_to(root) for root in roots) for read in reads):
            log.unlink(missing_ok=True)
            dropped += 1
    return dropped
