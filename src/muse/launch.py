"""Pass every heavy tool run, MuseScore or the optical cross-check, through 1 gate.

So 1 build process never runs more at once than its share of the cores.
"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import threading
import time
from typing import TYPE_CHECKING

import psutil

if TYPE_CHECKING:
    from collections.abc import Callable

SLOTS_VARIABLE = "MUSE_LAUNCH_SLOTS"
LAUNCHES = {"count": 0}
LAUNCH_SECONDS = 240


def slots() -> int:
    """Count the simultaneous launches this process may make.

    Returns:
        This process's share of simultaneous launches: what the build assigned it, else every core.

    """
    given = os.environ.get(SLOTS_VARIABLE, "")
    return max(1, int(given)) if given else max(1, os.cpu_count() or 1)


GATE = threading.BoundedSemaphore(slots())


FREE_CORE_WAIT = 3600.0
IDLE_WINDOW = 1.0


IDLE_LOCK = threading.Lock()
psutil.cpu_percent(interval=None)
IDLE_READING = {"at": time.monotonic(), "idle": 0.0}


def idle_percent() -> float:
    """Measure the share of all CPU time idle since the last reading, at most once per window.

    A reading younger than the window is shared, so no launch blocks to take its own.

    Returns:
        The idle share, measured, never the load average, which counts waiting threads.

    """
    with IDLE_LOCK:
        now = time.monotonic()
        if now - IDLE_READING["at"] >= IDLE_WINDOW:
            IDLE_READING["idle"] = 100.0 - float(psutil.cpu_percent(interval=None))
            IDLE_READING["at"] = now
        return IDLE_READING["idle"]


def free_core(
    deadline: float = FREE_CORE_WAIT,
    idle: Callable[[], float] = idle_percent,
    cores: int | None = None,
) -> None:
    """Wait until a whole core sits idle.

    The cores are shared with other work, and a run started on a saturated machine starves past its deadline;
    a saturated machine is read again once per window.

    Raises:
        TimeoutError: no whole core sat idle before the deadline.

    """
    total = cores or os.cpu_count() or 1
    give_up = time.monotonic() + deadline
    while (free := idle() * total / 100.0) < 1.0:
        if time.monotonic() > give_up:
            msg = f"no free core for {deadline:.0f} s: {free:.2f} of {total} cores idle"
            raise TimeoutError(msg)
        time.sleep(IDLE_WINDOW)


UNAVAILABLE = "the locked Mac refuses the tools a build needs: unlock it"
REFUSED_BY_LOCK = ("Error creating pasteboard", "Unable to compute the prediction using a neural network model")


class MachineUnavailableError(RuntimeError):
    """MuseScore aborted because the machine refused it a window session: no song is at fault."""


HANGS = pathlib.Path(tempfile.gettempdir()) / "muse-hangs"
SAMPLE_SECONDS = 3


def heavy(
    argv: list[str],
    timeout: float,
    *,
    check: bool = False,
    cwd: pathlib.Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run 1 heavy tool, MuseScore or the optical cross-check, waiting for a free slot first.

    The timeout counts only the run, never the wait; with check, a failed run raises;
    a run past its deadline is sampled before it is killed.

    Returns:
        The finished run with its exit status and captured output.

    Raises:
        TimeoutExpired: the run outlived its timeout; its stack was sampled first.
        MachineUnavailableError: the locked machine refused the tool a window session.

    """
    with GATE:
        free_core()
        LAUNCHES["count"] += 1
        with subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            stdin=subprocess.DEVNULL,
            cwd=cwd,
        ) as process:
            try:
                out, err = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired as expired:
                where = sample(process.pid)
                process.kill()
                process.communicate()
                raise subprocess.TimeoutExpired(argv, timeout, output=f"stack sampled to {where}") from expired
        done = subprocess.CompletedProcess(argv, process.returncode, out, err)
        if process.returncode and any(sign in err for sign in REFUSED_BY_LOCK):
            msg = f"{UNAVAILABLE} ({argv[0]} failed: {err.strip()[-200:]})"
            raise MachineUnavailableError(msg)
        if check:
            done.check_returncode()
        return done


def sample(pid: int) -> pathlib.Path | None:
    """Sample the native stack of a running process.

    The stack is written where a reader can find it after the process is gone.

    Returns:
        The file holding the stack; none where the sampler is absent.

    """
    tool = shutil.which("sample")
    if tool is None:
        return None
    HANGS.mkdir(parents=True, exist_ok=True)
    target = HANGS / f"{pid}-{int(time.time())}.sample"
    subprocess.run(
        [tool, str(pid), str(SAMPLE_SECONDS), "-file", str(target)],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=SAMPLE_SECONDS * 10,
        check=False,
    )
    return target


LOGS = pathlib.Path.home() / "Library" / "Application Support" / "MuseScore" / "MuseScore4" / "logs"
LOG_NAME = re.compile(r"MuseScore_(\d{6}_\d{6})\.log")
CONVERSION = re.compile(r"ConverterController::convertFile \| in: (.+?), out: ")


def mscore(
    args: list[str],
    timeout: float,
    command: str = "mscore",
    *,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Launch MuseScore once through the shared slots, its own logs removed after it.

    Returns:
        The finished MuseScore run.

    """
    began = time.time()
    try:
        return heavy([command, *args], timeout, check=check)
    finally:
        drop_own_logs(roots_of(args), began - 1, time.time() + 1)


def roots_of(args: list[str]) -> list[pathlib.Path]:
    """Find the folders a launch reads from.

    Returns:
        Each path argument's own folder, and for a job file, the folder of each path it lists.

    """
    paths = [pathlib.Path(arg) for arg in args if pathlib.Path(arg).exists()]
    for job in [path for path in paths if path.suffix == ".json"]:
        paths += [
            pathlib.Path(value)
            for value in re.findall(r'"([^"]+)"', job.read_text(encoding="utf-8"))
            if value.startswith("/") and pathlib.Path(value).exists()
        ]
    return sorted({path.resolve().parent for path in paths})


def drop_own_logs(roots: list[pathlib.Path], start: float, end: float, logs: pathlib.Path | None = None) -> int:
    """Delete each log named for a second between start and end whose every conversion reads a file under 1 of the roots.

    A log with no conversion, the owner's own session, stays.

    Returns:
        The number of logs deleted.

    """
    folder = logs or LOGS
    if not roots or not folder.is_dir():
        return 0
    dropped = 0
    for log in folder.iterdir():
        named = LOG_NAME.fullmatch(log.name)
        if not named or not start <= time.mktime(time.strptime(named[1], "%y%m%d_%H%M%S")) <= end:
            continue
        try:
            text = log.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            continue
        reads = CONVERSION.findall(text)
        if reads and all(any(pathlib.Path(read).resolve().is_relative_to(root) for root in roots) for read in reads):
            log.unlink(missing_ok=True)
            dropped += 1
    return dropped
