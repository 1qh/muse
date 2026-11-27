"""MuseScore renders gathered from many threads into 1 job per round.

A launch costs seconds, a render inside a job a fraction.
"""

from __future__ import annotations

import contextlib
import json
import pathlib
import subprocess
import tempfile
import threading
from typing import TYPE_CHECKING, NotRequired, TypedDict

from muse import launch
from muse.port import TransposeOptions

if TYPE_CHECKING:
    from collections.abc import Generator


RenderEntry = TypedDict(
    "RenderEntry",
    {"in": str, "out": list[str], "transpose": NotRequired[TransposeOptions]},
)


class RenderItem(TypedDict):
    ticket: int
    entry: RenderEntry
    outputs: list[pathlib.Path]


JOB_SECONDS = 900


class RenderFailedError(RuntimeError):
    """MuseScore wrote no output for a render it was given."""


class Batcher:
    """Each thread asks for a render and waits; once every working thread is waiting, 1 MuseScore job serves them all."""

    def __init__(self, command: str | None = None) -> None:
        self.command = command
        self.lock = threading.Condition()
        self.working = 0
        self.waiting = 0
        self.flushing = False
        self.pending: list[RenderItem] = []
        self.done: dict[int, str | launch.MachineUnavailableError | None] = {}
        self.ticket = 0
        self.jobs = 0

    @contextlib.contextmanager
    def worker(self) -> Generator[None, None, None]:
        """Count a thread that will ask for renders, so a round waits for it."""
        with self.lock:
            self.working += 1
        try:
            yield
        finally:
            with self.lock:
                self.working -= 1
                self.lock.notify_all()

    def render(self, score: pathlib.Path, outputs: list[pathlib.Path], transpose: TransposeOptions | None = None) -> None:
        """Write every output of 1 score, waiting for the job that carries it.

        Raises:
            RenderFailedError: MuseScore wrote no output for this score.
            ValueError: an output is the score itself, which the render would delete before reading.

        """
        if score.resolve() in {out.resolve() for out in outputs}:
            msg = f"render output {score} is its own input"
            raise ValueError(msg)
        for out in outputs:
            out.unlink(missing_ok=True)
        entry: RenderEntry = {"in": str(score), "out": [str(out) for out in outputs]}
        if transpose is not None:
            entry["transpose"] = transpose
        with self.lock:
            self.ticket += 1
            ticket = self.ticket
            self.pending.append({"ticket": ticket, "entry": entry, "outputs": outputs})
            self.waiting += 1
            try:
                while ticket not in self.done:
                    if not self.flushing and self.pending and self.waiting >= max(1, self.working):
                        batch, self.pending, self.flushing = self.pending, [], True
                        self._flush(batch, score)
                    else:
                        self.lock.wait(timeout=1.0)
                failure = self.done.pop(ticket)
            finally:
                self.waiting -= 1
        if isinstance(failure, launch.MachineUnavailableError):
            raise failure
        if failure is not None and failure:
            raise RenderFailedError(failure)

    def _flush(self, batch: list[RenderItem], score: pathlib.Path) -> None:
        """Run a batch outside the lock and record a result for every waiting ticket."""
        self.lock.release()
        results: dict[int, str | launch.MachineUnavailableError | None] = {}
        try:
            with launch.disk_faults(score):
                results.update(self.run(batch))
        except launch.MachineUnavailableError as error:
            results.update({item["ticket"]: error for item in batch})
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            results.update({item["ticket"]: f"job failed: {error}" for item in batch})
        finally:
            self.lock.acquire()
            self.flushing = False
            self.lock.notify_all()
        self.done.update(results)
        self.jobs += 1

    def run(self, batch: list[RenderItem]) -> dict[int, str | None]:
        """1 MuseScore job for the batch; each render's verdict is whether its outputs exist.

        Returns:
            Each ticket's failure note, or None where every output exists.

        """
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as handle:
            json.dump([item["entry"] for item in batch], handle)
            job = pathlib.Path(handle.name)
        try:
            note = launch.served(job, JOB_SECONDS, command=self.command)
        except subprocess.TimeoutExpired as expired:
            note = f"job of {len(batch)} renders passed {JOB_SECONDS} seconds: {expired.output}"
        finally:
            job.unlink(missing_ok=True)
        results: dict[int, str | None] = {}
        for item in batch:
            missing = [str(out) for out in item["outputs"] if not out.exists()]
            results[item["ticket"]] = f"no {missing}: {note}" if missing else None
        return results


BATCHER = Batcher()
