"""Pass every heavy tool run, MuseScore or the optical cross-check, through 1 gate.

So 1 build process never runs more at once than its share of the cores.
"""

from __future__ import annotations

import json
import os
import pathlib
import queue
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
MSCORE_VARIABLE = "MUSE_MSCORE"


def mscore_command() -> str:
    """Name the MuseScore every run uses: the fork's build, named by MUSE_MSCORE.

    Returns:
        The MuseScore executable.

    Raises:
        RuntimeError: MUSE_MSCORE is unset or names no executable.

    """
    given = os.environ.get(MSCORE_VARIABLE, "")
    if not given or not os.access(given, os.X_OK):
        msg = f"{MSCORE_VARIABLE} must name the fork's mscore executable (1qh/MuseScore); got {given!r}"
        raise RuntimeError(msg)
    return given


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
    command: str | None = None,
    *,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Launch MuseScore once through the shared slots, its own logs removed after it.

    Returns:
        The finished MuseScore run.

    """
    began = time.time()
    try:
        return heavy([command or mscore_command(), *args], timeout, check=check)
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


SERVE_FLAG = "--serve"


class ServerDiedError(RuntimeError):
    """A served MuseScore exited while a request was in flight."""


class Server:
    """1 running `mscore --serve`, answering 1 job per request line."""

    def __init__(self, command: str) -> None:
        self.process = subprocess.Popen(
            [command, SERVE_FLAG],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.answers: queue.Queue[str | None] = queue.Queue()
        self.errors: list[str] = []
        self.number = 0
        threading.Thread(target=self._read_answers, daemon=True).start()
        threading.Thread(target=self._read_errors, daemon=True).start()

    def _read_answers(self) -> None:
        stream = self.process.stdout
        if stream is not None:
            for line in stream:
                self.answers.put(line.rstrip("\n"))
        self.answers.put(None)

    def _read_errors(self) -> None:
        stream = self.process.stderr
        if stream is not None:
            for line in stream:
                self.errors.append(line)
                del self.errors[:-STDERR_KEPT]

    def run(self, job: pathlib.Path, extension: str | None, timeout: float) -> str:
        """Send 1 job and wait for its answer.

        Returns:
            Empty when the job succeeded, else MuseScore's reason.

        Raises:
            TimeoutExpired: the job outlived its timeout; the server was sampled and stopped.
            ServerDiedError: the server exited before answering; a locked machine reports MachineUnavailableError.

        """
        self.number += 1
        ident = str(self.number)
        request: dict[str, str] = {"id": ident, "job": str(job)}
        if extension is not None:
            request["extension"] = extension
        stdin = self.process.stdin
        if stdin is None:
            msg = "served MuseScore has no input"
            raise ServerDiedError(msg)
        try:
            stdin.write(json.dumps(request) + "\n")
            stdin.flush()
        except BrokenPipeError as error:
            raise self._died() from error
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            try:
                line = self.answers.get(timeout=max(0.0, left))
            except queue.Empty as error:
                where = sample(self.process.pid)
                self.stop()
                raise subprocess.TimeoutExpired(
                    [SERVE_FLAG, str(job)],
                    timeout,
                    output=f"stack sampled to {where}",
                ) from error
            if line is None:
                raise self._died()
            if line.startswith(ident + " "):
                answer = line[len(ident) + 1 :]
                return "" if answer == "ok" else answer.removeprefix("failed ")

    def _died(self) -> Exception:
        self.process.wait(timeout=10)
        note = "".join(self.errors).strip()[-300:]
        if any(sign in note for sign in REFUSED_BY_LOCK):
            return MachineUnavailableError(f"{UNAVAILABLE} (mscore failed: {note})")
        return ServerDiedError(f"served MuseScore exited {self.process.returncode}: {note}")

    def alive(self) -> bool:
        return self.process.poll() is None

    def stop(self) -> None:
        if self.alive():
            self.process.kill()
        self.process.wait(timeout=10)


STDERR_KEPT = 40
SERVERS: queue.Queue[Server] = queue.Queue()
STARTED = {"count": 0}
SERVERS_LOCK = threading.Lock()


def require_serve(command: str | None = None) -> None:
    """Refuse a MuseScore without the fork's serve mode.

    Raises:
        RuntimeError: the MuseScore on the path cannot serve.

    """
    found = command or mscore_command()
    shown = subprocess.run([found, "--help"], capture_output=True, text=True, stdin=subprocess.DEVNULL, check=False)
    if SERVE_FLAG not in shown.stdout:
        msg = f"{found} has no {SERVE_FLAG}: install the fork's MuseScore (1qh/MuseScore)"
        raise RuntimeError(msg)


def served(job: pathlib.Path, timeout: float, extension: str | None = None, command: str | None = None) -> str:
    """Run 1 job on a pooled served MuseScore, starting one while fewer run than the slots allow.

    Returns:
        Empty when the job succeeded, else MuseScore's reason.

    """
    with SERVERS_LOCK:
        if SERVERS.empty() and STARTED["count"] < slots():
            SERVERS.put(Server(command or mscore_command()))
            STARTED["count"] += 1
    server = SERVERS.get()
    LAUNCHES["count"] += 1
    try:
        answer = server.run(job, extension, timeout)
    except BaseException:
        server.stop()
        with SERVERS_LOCK:
            STARTED["count"] -= 1
        raise
    SERVERS.put(server)
    return answer
