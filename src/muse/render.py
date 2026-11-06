"""MuseScore renders gathered from many threads into 1 job per round: a launch costs seconds, a render inside a job a fraction."""
from __future__ import annotations

import contextlib
import json
import pathlib
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator
from typing import Any

JOB_SECONDS = 900


class RenderFailed(RuntimeError):
    """MuseScore wrote no output for a render it was given."""


class Batcher:
    """Each thread asks for a render and waits; once every working thread is waiting, 1 MuseScore job serves them all."""

    def __init__(self, command: str = "mscore") -> None:
        self.command = command
        self.lock = threading.Condition()
        self.working = 0
        self.waiting = 0
        self.flushing = False
        self.pending: list[dict[str, Any]] = []
        self.done: dict[int, str | None] = {}
        self.ticket = 0
        self.jobs = 0
        self.renders = 0
        self.seconds = 0.0  # wall time spent inside MuseScore jobs

    @contextlib.contextmanager
    def worker(self) -> Iterator[None]:
        """Count a thread that will ask for renders, so a round waits for it."""
        with self.lock:
            self.working += 1
        try:
            yield
        finally:
            with self.lock:
                self.working -= 1
                self.lock.notify_all()

    def render(self, score: pathlib.Path, outputs: list[pathlib.Path], transpose: dict[str, Any] | None = None) -> None:
        """Write every output of 1 score, waiting for the job that carries it."""
        for out in outputs:
            out.unlink(missing_ok=True)
        entry: dict[str, Any] = {"in": str(score), "out": [str(out) for out in outputs]}
        if transpose:
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
                        self.lock.release()
                        began = time.monotonic()
                        try:
                            results = self.run(batch)
                        finally:
                            self.lock.acquire()
                            self.seconds += time.monotonic() - began
                        self.done.update(results)
                        self.jobs += 1
                        self.renders += len(batch)
                        self.flushing = False
                        self.lock.notify_all()
                    else:
                        self.lock.wait(timeout=1.0)
                failure = self.done.pop(ticket)
            finally:
                self.waiting -= 1
        if failure:
            raise RenderFailed(failure)

    def run(self, batch: list[dict[str, Any]]) -> dict[int, str | None]:
        """1 MuseScore job for the batch; each render's verdict is whether its outputs exist."""
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump([item["entry"] for item in batch], handle)
            job = pathlib.Path(handle.name)
        try:
            done = subprocess.run([self.command, "-j", str(job)], capture_output=True, text=True, timeout=JOB_SECONDS, stdin=subprocess.DEVNULL, check=False)
            note = done.stderr[-300:]
        except subprocess.TimeoutExpired:
            note = f"job of {len(batch)} renders passed {JOB_SECONDS} seconds"
        finally:
            job.unlink(missing_ok=True)
        results: dict[int, str | None] = {}
        for item in batch:
            missing = [str(out) for out in item["outputs"] if not out.exists()]
            results[item["ticket"]] = f"no {missing}: {note}" if missing else None
        return results


BATCHER = Batcher()
