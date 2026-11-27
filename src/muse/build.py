"""Build every out-of-date file of each song: the calculated layout per sheet, the build record, the report."""

from __future__ import annotations

import concurrent.futures
import contextlib
import faulthandler
import json
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import threading
import time
from time import monotonic as _monotonic
from typing import TYPE_CHECKING, Protocol

import psutil

from muse import buildcache, chordnames, fullscore, gate, launch, layout, mode, piano, port, publish, toolpath
from muse.song import decode, song_of
from muse.song import sources as song_sources

if TYPE_CHECKING:
    from collections.abc import Iterator

    from muse.model import Score


Decision = buildcache.Decision


Sheet = tuple[str, dict[str, buildcache.CacheRecord], dict[str, object], list[str]]
CHILD_REQUIRED_ARGS = 3
CHILD_ARGUMENT_OFFSET = 2


def variant_key(src: pathlib.Path, variant: str) -> str:
    """Compute the build key of 1 sheet: its source, settings and every file of code that can shape it.

    Returns:
        The build key.

    """
    del variant
    return buildcache.key_of(src, repr(song_of(src)), buildcache.sheet_code(), mode.label())


def decision(found: tuple[Score, pathlib.Path, float, float, int]) -> Decision:
    """Record what a sheet's layout settled, kept in its build record.

    Returns:
        The settled spatium, distance, pages and line starts.

    """
    return {"spatium": found[2], "distance": found[3], "pages": found[4], "lines": port.line_starts(found[0])}


def _finish(report: dict[str, object], built: list[str], began: float, work: pathlib.Path, name: str) -> None:
    report["built"] = built
    report["launches"] = launch.LAUNCHES["count"]
    report["seconds"] = round(time.monotonic() - began, 1)
    (work / f"{name}-report.json").write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")


def _orchestral(
    src: pathlib.Path,
    out_dir: pathlib.Path,
    work: pathlib.Path,
    sheet: Sheet,
) -> None:
    _, state, report, built = sheet
    info = song_of(src)
    outputs = [out_dir / info.score_file, out_dir / info.original_pdf]
    key = variant_key(src, port.ORIGINAL)
    if not buildcache.fresh(state.get(port.ORIGINAL), key, outputs):
        report.update(fullscore.build(src, out_dir, work, info))
        state[port.ORIGINAL] = buildcache.record(
            key,
            [path for path in outputs if path.exists()],
            decision={"spatium": fullscore.SPATIUM, "distance": fullscore.DISTANCE},
        )
        buildcache.save(info.name, state)
        built.append(port.ORIGINAL)


def _current(
    src: pathlib.Path,
    state: dict[str, buildcache.CacheRecord],
    variant: str,
    outputs: list[pathlib.Path],
) -> tuple[str, bool]:
    key = variant_key(src, variant)
    return key, buildcache.fresh(state.get(variant), key, outputs)


KEEP_LOCK = threading.Lock()


def _keep(
    sheet: Sheet,
    variant: str,
    key: str,
    outputs: list[pathlib.Path],
    found: tuple[Score, pathlib.Path, float, float, int],
) -> None:
    name, state, report, built = sheet
    with KEEP_LOCK:
        state[variant] = buildcache.record(key, outputs, decision=decision(found))
        buildcache.save(name, state)
        report[variant] = (found[2], found[3], found[4])
        built.append(variant)


def _laid_pdf(found: tuple[Score, pathlib.Path, float, float, int], pdf: pathlib.Path, pair: pathlib.Path) -> None:
    shutil.copyfile(found[1].with_name(found[1].stem + "-layout.pdf"), pdf)
    port.keep_pair(found[1].with_name(found[1].stem + "-layout.mpos"), pdf, pair)


def _original(
    src: pathlib.Path,
    out_dir: pathlib.Path,
    work: pathlib.Path,
    sheet: Sheet,
) -> None:
    info = song_of(src)
    outputs = [
        out_dir / info.score_file,
        out_dir / info.original_pdf,
        port.paired(work, info.original_pdf),
        port.paired(work, info.original_pdf).with_suffix(".sha"),
    ]
    key, ready = _current(src, sheet[1], port.ORIGINAL, outputs)
    if not ready:
        found = layout.lay(src, work, port.ORIGINAL)
        (out_dir / info.score_file).write_bytes(found[1].read_bytes())
        _laid_pdf(found, out_dir / info.original_pdf, port.paired(work, info.original_pdf))
        chordnames.drop_chord_path(out_dir / info.score_file)
        _keep(sheet, port.ORIGINAL, key, outputs, found)


def _tone(
    src: pathlib.Path,
    out_dir: pathlib.Path,
    work: pathlib.Path,
    sheet: Sheet,
) -> None:
    info = song_of(src)
    tone_score = work / f"{info.name}-{port.TONE}.mscz"
    outputs = [
        out_dir / info.tone_pdf,
        tone_score,
        port.paired(work, info.tone_pdf),
        port.paired(work, info.tone_pdf).with_suffix(".sha"),
    ]
    key, ready = _current(src, sheet[1], port.TONE, outputs)
    if not ready:
        found = layout.lay(src, work, port.TONE)
        _laid_pdf(found, out_dir / info.tone_pdf, port.paired(work, info.tone_pdf))
        shutil.copyfile(found[1], tone_score)
        _keep(sheet, port.TONE, key, outputs, found)


def _piano(
    src: pathlib.Path,
    out_dir: pathlib.Path,
    work: pathlib.Path,
    sheet: Sheet,
) -> None:
    info = song_of(src)
    final = work / f"{info.name}-piano-final.mscz"
    outputs = [
        out_dir / info.piano_pdf,
        final,
        port.paired(work, info.piano_pdf),
        port.paired(work, info.piano_pdf).with_suffix(".sha"),
    ]
    key, ready = _current(src, sheet[1], port.PIANO, outputs)
    if not ready:
        found = layout.lay(src, work, port.PIANO)
        _laid_pdf(found, out_dir / info.piano_pdf, port.paired(work, info.piano_pdf))
        final.write_bytes(found[1].read_bytes())
        _keep(sheet, port.PIANO, key, outputs, found)


def song(src: pathlib.Path, out_dir: pathlib.Path, work: pathlib.Path) -> dict[str, object]:
    """Build every out-of-date file of 1 song's folder.

    Returns:
        What was built, for the report.

    """
    began = time.monotonic()
    launch.LAUNCHES["count"] = 0
    info = song_of(src)
    work.mkdir(parents=True, exist_ok=True)
    (out_dir / info.name).mkdir(parents=True, exist_ok=True)
    report: dict[str, object] = {"song": info.name}
    state = buildcache.load(info.name)
    built: list[str] = []
    if info.orchestral:
        _orchestral(src, out_dir, work, (info.name, state, report, built))
        _finish(report, built, began, work, info.name)
        return report
    sheet = (info.name, state, report, built)
    builders = [_original, *([_tone] if info.direction else []), _piano]
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(builders)) as sheets:
        for done in [sheets.submit(builder, src, out_dir, work, sheet) for builder in builders]:
            done.result()
    found_parts = piano.parts(decode(src))
    probe_score = decode(src)
    report["parts"] = [
        (p.name, p.first, p.last, piano.staff_of([probe_score.bars[n - 1] for n in range(p.first, p.last + 1)]))
        for p in found_parts
    ]
    _finish(report, built, began, work, info.name)
    return report


def free_workers(count: int, cores: int | None = None) -> int:
    """Count the workers: 1 per song up to the cores, at least 1.

    Launch slots, never a load sample, keep heavy runs within the cores.

    Returns:
        The number of workers.

    """
    cores = cores if cores is not None else os.cpu_count() or 1
    return max(1, min(count, port.MAX_WORKERS, cores))


PRIORITY = "PRIORITY.txt"


def ordered(sources: list[pathlib.Path]) -> list[pathlib.Path]:
    """Order the owner's priority songs first, in their order, then the rest by name.

    Returns:
        The sources in build order.

    """
    path = pathlib.Path(os.environ["MUSE_PRIVATE"]) / PRIORITY
    first = path.read_text(encoding="utf-8").split() if path.exists() else []
    rank = {name: at for at, name in enumerate(first)}
    return sorted(sources, key=lambda src: (rank.get(song_of(src).name, len(rank)), song_of(src).name))


def publish_song(src_dir: pathlib.Path, out_dir: pathlib.Path, work: pathlib.Path, stage: pathlib.Path, name: str) -> str:
    """Copy 1 finished song from its staging folder into the sheets repo and publish it alone.

    Returns:
        The reason it was blocked, or empty.

    """
    publish.resync(out_dir)
    shutil.rmtree(out_dir / name, ignore_errors=True)
    shutil.copytree(stage / name, out_dir / name)
    blocked = publish.publish(publish.Places(src_dir, out_dir, work), names=(name,), purging=False)
    return "; ".join(blocked.get(name, [])[:3])


CHILD_FLAG = "--song"
MACHINE_FAULT_PREFIX = "machine fault: "
CHILD_REFUSED_EXIT = 2
RUNNING: set[subprocess.Popen[bytes]] = set()
RUNNING_LOCK = threading.Lock()
STOPPED_CHECK_SECONDS = 3.0


class ProcessStatus(Protocol):
    """Process details needed to describe a stopped worker or descendant."""

    @property
    def pid(self) -> int:
        """The process identifier.

        Args:
            self: The inspected process.

        Returns:
            Its process identifier.

        """

    def status(self) -> str:
        """Read the process state.

        Args:
            self: The inspected process.

        Returns:
            Its current state.

        """

    def ppid(self) -> int:
        """Read the parent identifier.

        Args:
            self: The inspected process.

        Returns:
            Its parent's process identifier.

        """

    def cmdline(self) -> list[str]:
        """Read the command arguments.

        Args:
            self: The inspected process.

        Returns:
            Its command line arguments.

        """


def stopped_process_lines[P: ProcessStatus](processes: list[P], elapsed: float) -> list[str]:
    """Describe stopped processes without writing output or changing process state.

    Args:
        processes: Workers or descendants to inspect.
        elapsed: Seconds since the build began.

    Returns:
        One line per stopped process whose details remain accessible.

    """
    lines: list[str] = []
    for process in processes:
        try:
            if process.status() == psutil.STATUS_STOPPED:
                lines.append(
                    f"pid {process.pid} parent pid {process.ppid()} cmdline {json.dumps(process.cmdline())} "
                    f"build elapsed {elapsed:.1f}s: stopped (state T): send SIGCONT to resume, or find the sender",
                )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return lines


def _warn_stopped(reported: set[psutil.Process], elapsed: float) -> None:
    """Write each stopped worker or recursive descendant to stderr once.

    Args:
        reported: Process identities already reported during this build.
        elapsed: Seconds since the build began.

    """
    with RUNNING_LOCK:
        workers = list(RUNNING)
    processes: list[psutil.Process] = []
    for worker in workers:
        if worker.poll() is None:
            with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                process = psutil.Process(worker.pid)
                processes.append(process)
                processes.extend(process.children(recursive=True))
    for process in processes:
        if process not in reported:
            lines = stopped_process_lines([process], elapsed)
            if lines:
                sys.stderr.write("\n".join(lines) + "\n")
                sys.stderr.flush()
                reported.add(process)


def _completed(
    jobs: dict[concurrent.futures.Future[tuple[dict[str, object] | None, str]], pathlib.Path],
    began: float,
) -> Iterator[concurrent.futures.Future[tuple[dict[str, object] | None, str]]]:
    """Yield finished jobs while periodically checking the running process trees.

    Args:
        jobs: Song jobs being collected.
        began: Monotonic time when the build began.

    Yields:
        Each completed song job.

    """
    pending = set(jobs)
    reported: set[psutil.Process] = set()
    next_check = _monotonic()
    while pending:
        now = _monotonic()
        if now >= next_check:
            _warn_stopped(reported, now - began)
            next_check = _monotonic() + STOPPED_CHECK_SECONDS
        done, pending = concurrent.futures.wait(
            pending,
            timeout=max(0.0, next_check - _monotonic()),
            return_when=concurrent.futures.FIRST_COMPLETED,
        )
        yield from done


def stop_children() -> None:
    """Kill every song worker still running, each with the MuseScore runs it started."""
    with RUNNING_LOCK:
        running = list(RUNNING)
    for process in running:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)


def child(
    src: pathlib.Path,
    stage: pathlib.Path,
    work: pathlib.Path,
    gated_from: pathlib.Path | None = None,
) -> tuple[dict[str, object] | None, str]:
    """Build 1 song in its own child process, and gate its staged folder there when `gated_from` names the sources.

    Gating in the child runs every song's gate at once; publishing then finds the recorded pass for the same bytes.

    Returns:
        Its report, or none and the reason the child wrote.

    Raises:
        TypeError: The child's report is not a JSON object.
        ToolMissingError: The child process could not start.

    """
    name = song_of(src).name
    refusal = work / f"{name}-refusal.txt"
    refusal.unlink(missing_ok=True)
    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "muse.build", CHILD_FLAG, str(src), str(stage), str(work)]
            + ([str(gated_from)] if gated_from is not None else []),
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as error:
        msg = f"could not start tool {sys.executable!r}: {error}"
        raise launch.ToolMissingError(msg) from error
    with RUNNING_LOCK:
        RUNNING.add(process)
    try:
        code = process.wait()
    finally:
        with RUNNING_LOCK:
            RUNNING.discard(process)
    if code == 0:
        loaded = json.loads((work / f"{name}-report.json").read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            msg = f"{name}: the build report is not a JSON object"
            raise TypeError(msg)
        return {str(field): value for field, value in loaded.items()}, ""
    return None, refusal.read_text(
        encoding="utf-8",
    ) if refusal.exists() else f"child exited {code} with no reason"


def child_main(src: pathlib.Path, stage: pathlib.Path, work: pathlib.Path, gated_from: pathlib.Path | None = None) -> None:
    """Build 1 song as the child, its report written by song(); a refusal written to its own file and a non-zero exit.

    Raises:
        SystemExit: the song was refused; the reason is in its refusal file.

    """
    try:
        song(src, stage, work)
        if gated_from is not None:
            gate.verdicts(gated_from, stage, work, (song_of(src).name,))
    except launch.MachineUnavailableError as error:
        (work / f"{song_of(src).name}-refusal.txt").write_text(
            f"{MACHINE_FAULT_PREFIX}{error}",
            encoding="utf-8",
        )
        raise SystemExit(CHILD_REFUSED_EXIT) from error
    except (port.DoesNotFitError, RuntimeError, OSError, subprocess.SubprocessError, ValueError) as error:
        (work / f"{song_of(src).name}-refusal.txt").write_text(str(error), encoding="utf-8")
        raise SystemExit(CHILD_REFUSED_EXIT) from error


def _require_tools(*, publishing: bool) -> None:
    launch.require_mscore()
    if not publishing:
        return
    launch.require_tools(
        ("git", toolpath.which("git")),
        ("git-filter-repo", toolpath.which("git-filter-repo")),
        ("homr", gate.homr_command()),
        ("pdftotext", toolpath.which("pdftotext")),
    )


def _say(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def _collect(
    pool: concurrent.futures.ThreadPoolExecutor,
    jobs: dict[concurrent.futures.Future[tuple[dict[str, object] | None, str]], pathlib.Path],
    places: tuple[pathlib.Path, pathlib.Path, pathlib.Path, pathlib.Path, bool],
    tallies: tuple[list[dict[str, object]], list[str], list[str]],
    began: float | None = None,
) -> None:
    src_dir, out_dir, work, stage, publishing = places
    reports, refused, blocked = tallies
    for job in _completed(jobs, time.monotonic() if began is None else began):
        src = jobs[job]
        if (error := job.exception()) is not None:
            report, reason = None, f"its build raised {error!r}"
        else:
            report, reason = job.result()
        if reason.startswith(MACHINE_FAULT_PREFIX) or launch.UNAVAILABLE in reason:
            pool.shutdown(wait=False, cancel_futures=True)
            raise SystemExit(reason)
        if report is None:
            refused.append(src.stem)
            _say(f"refused {src.stem}: {reason}")
            continue
        reports.append(report)
        _say(json.dumps(report, ensure_ascii=False))
        if publishing:
            try:
                reason = publish_song(src_dir, out_dir, work, stage, song_of(src).name)
            except launch.MachineUnavailableError as error:
                pool.shutdown(wait=False, cancel_futures=True)
                raise SystemExit(str(error)) from error
            except publish.RefusedError as error:
                reason = f"publishing failed: {error}"
            _say(f"{'blocked' if reason else 'published'} {song_of(src).name} {reason}".rstrip())
            if reason:
                blocked.append(song_of(src).name)


def _record(work: pathlib.Path, reports: list[dict[str, object]], began: float) -> None:
    previous = work / "build-report.json"
    earlier = json.loads(previous.read_text(encoding="utf-8")) if previous.exists() else {}
    earlier.update({str(r["song"]): r for r in reports})
    previous.write_text(json.dumps(earlier, ensure_ascii=False, indent=1), encoding="utf-8")
    launches = sum(int(str(r["launches"])) for r in reports)
    sys.stdout.write(f"songs {len(reports)} launches {launches} seconds {time.monotonic() - began:.1f}" + "\n")


def _run(
    sources: list[pathlib.Path],
    workers: int,
    places: tuple[pathlib.Path, pathlib.Path, pathlib.Path, pathlib.Path, bool],
    tallies: tuple[list[dict[str, object]], list[str], list[str]],
    began: float,
) -> None:
    src_dir, out_dir, work, stage, publishing = places
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
    try:
        jobs = {pool.submit(child, src, stage, work, src_dir if publishing else None): src for src in sources}
        _collect(pool, jobs, places, tallies, began)
        if publishing:
            publish.purge(out_dir)
    except BaseException as error:
        _say(f"stopped: {error!r}")
        stop_children()
        raise
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


def main() -> None:
    launch.ignore_terminal_stops()
    publishing = "--publish" in sys.argv[1:]
    child_mode = sys.argv[1:2] == [CHILD_FLAG]
    _require_tools(publishing=publishing)
    if child_mode and len(sys.argv[CHILD_ARGUMENT_OFFSET:]) > CHILD_REQUIRED_ARGS:
        launch.require_tools(("homr", gate.homr_command()), ("pdftotext", toolpath.which("pdftotext")))
    faulthandler.register(signal.SIGUSR1, all_threads=True)
    if sys.argv[1:2] == [CHILD_FLAG]:
        child_main(*map(pathlib.Path, sys.argv[2:6]))
        return
    port.require_font()
    launch.require_serve()
    args = [arg for arg in sys.argv[1:] if arg != "--publish"]
    src_dir, out_dir, work = map(pathlib.Path, args[:3])
    wanted = args[3:]
    sources = ordered([src for src in song_sources(src_dir) if not wanted or src.stem.split("-")[0] in wanted])
    stage = work / "stage" if publishing else out_dir
    began = time.monotonic()
    reports: list[dict[str, object]] = []
    refused: list[str] = []
    blocked: list[str] = []
    workers = free_workers(len(sources))
    os.environ[launch.SLOTS_VARIABLE] = str(max(1, (os.cpu_count() or 1) // workers))
    layout.install_extension()
    _run(sources, workers, (src_dir, out_dir, work, stage, publishing), (reports, refused, blocked), began)
    _record(work, reports, began)
    if refused or blocked:
        msg = f"refused: {refused} blocked: {blocked}"
        raise SystemExit(msg)


if __name__ == "__main__":
    main()
