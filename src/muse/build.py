"""Build every out-of-date file of each song: the calculated layout per sheet, the build record, the report."""

from __future__ import annotations

import concurrent.futures
import faulthandler
import json
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import time
from typing import Any

from muse import buildcache, chordnames, gate, launch, layout, mode, piano, port, publish, scorefile
from muse.model import Score
from muse.song import LEGACY_FILES, decode, song_of
from muse.song import sources as song_sources


def variant_key(src: pathlib.Path, variant: str) -> str:
    """The build key of 1 sheet: its source, settings and every file of code that can shape it."""
    skip = buildcache.CHECK_ONLY_CODE if variant == port.PIANO else buildcache.PIANO_ONLY_CODE | buildcache.CHECK_ONLY_CODE
    return buildcache.key_of(src, repr(song_of(src)), buildcache.code_files(skip=skip), mode.label())


def decision(found: tuple[Score, pathlib.Path, float, float, int]) -> dict[str, Any]:
    """What a sheet's layout settled, kept in its build record."""
    return {"spatium": found[2], "distance": found[3], "pages": found[4], "lines": port.line_starts(found[0])}


def song(src: pathlib.Path, out_dir: pathlib.Path, work: pathlib.Path) -> dict[str, object]:
    """Every out-of-date file of 1 song's folder; returns what was built for the report."""
    began = time.monotonic()
    launch.LAUNCHES["count"] = 0
    info = song_of(src)
    work.mkdir(parents=True, exist_ok=True)
    (out_dir / info.name).mkdir(parents=True, exist_ok=True)
    report: dict[str, object] = {"song": info.name}
    state = buildcache.load(info.name)
    built = []

    def current(variant: str, outputs: list[pathlib.Path]) -> tuple[str, bool]:
        key = variant_key(src, variant)
        return key, buildcache.fresh(state.get(variant), key, outputs)

    def keep(variant: str, key: str, outputs: list[pathlib.Path], found: tuple[Score, pathlib.Path, float, float, int]) -> None:
        state[variant] = buildcache.record(key, outputs, decision=decision(found))
        buildcache.save(info.name, state)
        report[variant] = (found[2], found[3], found[4])
        built.append(variant)

    outputs = [out_dir / info.score_file, out_dir / info.original_pdf] + ([out_dir / info.legacy_file] if LEGACY_FILES else [])
    key, ready = current(port.ORIGINAL, outputs)
    if not ready:
        found = layout.lay(src, work, port.ORIGINAL)
        (out_dir / info.score_file).write_bytes(found[1].read_bytes())
        shutil.copyfile(found[1].with_name(found[1].stem + "-layout.pdf"), out_dir / info.original_pdf)
        if LEGACY_FILES:
            # the legacy file reads the original tone's MusicXML, so it is built before anything overwrites it
            port.build_legacy(src, work, out_dir / info.legacy_file, found[2], found[3], info)
            chordnames.drop_chord_path(out_dir / info.score_file)
            chordnames.drop_chord_path(out_dir / info.legacy_file)
            legacy_gap = scorefile.difference(out_dir / info.score_file, out_dir / info.legacy_file)
            report["legacy"] = legacy_gap[:6]
            if legacy_gap:
                (out_dir / info.legacy_file).unlink()
                outputs = outputs[:2]
        else:
            chordnames.drop_chord_path(out_dir / info.score_file)
            (out_dir / info.legacy_file).unlink(missing_ok=True)
        keep(port.ORIGINAL, key, outputs, found)
    if info.direction:
        tone_score = work / f"{info.name}-{port.TONE}.mscz"  # the gate reads the tone sheet's own score
        outputs = [out_dir / info.tone_pdf, tone_score]
        key, ready = current(port.TONE, outputs)
        if not ready:
            found = layout.lay(src, work, port.TONE)
            shutil.copyfile(found[1].with_name(found[1].stem + "-layout.pdf"), out_dir / info.tone_pdf)
            shutil.copyfile(found[1], tone_score)
            keep(port.TONE, key, outputs, found)
    final = work / f"{info.name}-piano-final.mscz"
    outputs = [out_dir / info.piano_pdf, final]
    key, ready = current(port.PIANO, outputs)
    if not ready:
        found = layout.lay(src, work, port.PIANO)
        shutil.copyfile(found[1].with_name(found[1].stem + "-layout.pdf"), out_dir / info.piano_pdf)
        final.write_bytes(found[1].read_bytes())
        keep(port.PIANO, key, outputs, found)
    found_parts = piano.parts(decode(src))
    probe_score = decode(src)
    report["parts"] = [(p.name, p.first, p.last, piano.staff_of([probe_score.bars[n - 1] for n in range(p.first, p.last + 1)])) for p in found_parts]
    report["built"] = built
    report["launches"] = launch.LAUNCHES["count"]
    report["seconds"] = round(time.monotonic() - began, 1)
    (work / f"{info.name}-report.json").write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    return report


def free_workers(count: int, cores: int | None = None) -> int:
    """1 worker per song up to the cores, at least 1; launch slots, never a load sample, keep heavy runs within the cores."""
    cores = cores if cores is not None else os.cpu_count() or 1
    return max(1, min(count, port.MAX_WORKERS, cores))


PRIORITY = "PRIORITY.txt"  # in the private repo: song names to build first, in order


def ordered(sources: list[pathlib.Path]) -> list[pathlib.Path]:
    """The owner's priority songs first, in their order, then the rest by name."""
    path = pathlib.Path(os.environ["MUSE_PRIVATE"]) / PRIORITY
    first = path.read_text(encoding="utf-8").split() if path.exists() else []
    rank = {name: at for at, name in enumerate(first)}
    return sorted(sources, key=lambda src: (rank.get(song_of(src).name, len(rank)), song_of(src).name))


def publish_song(src_dir: pathlib.Path, out_dir: pathlib.Path, work: pathlib.Path, stage: pathlib.Path, name: str) -> str:
    """1 finished song copied from its staging folder into the sheets repo and published alone; the reason it was blocked, or empty."""
    shutil.rmtree(out_dir / name, ignore_errors=True)
    shutil.copytree(stage / name, out_dir / name)
    blocked = publish.publish(src_dir, out_dir, work, names=(name,))
    return "; ".join(blocked.get(name, [])[:3])


CHILD_FLAG = "--song"


def child(src: pathlib.Path, stage: pathlib.Path, work: pathlib.Path) -> tuple[dict[str, object] | None, str]:
    """1 song built in its own child process: its report, or none and the reason the child wrote."""
    name = song_of(src).name
    refusal = work / f"{name}-refusal.txt"
    refusal.unlink(missing_ok=True)
    done = subprocess.run([sys.executable, "-m", "muse.build", CHILD_FLAG, str(src), str(stage), str(work)], stdin=subprocess.DEVNULL, check=False)
    if done.returncode == 0:
        return json.loads((work / f"{name}-report.json").read_text(encoding="utf-8")), ""
    return None, refusal.read_text(encoding="utf-8") if refusal.exists() else f"child exited {done.returncode} with no reason"


def child_main(src: pathlib.Path, stage: pathlib.Path, work: pathlib.Path) -> None:
    """The child: build 1 song, its report written by song(); a refusal written to its own file and a non-zero exit."""
    try:
        song(src, stage, work)
    except (port.DoesNotFit, RuntimeError, OSError, subprocess.SubprocessError, ValueError) as error:
        (work / f"{song_of(src).name}-refusal.txt").write_text(str(error), encoding="utf-8")
        raise SystemExit(2) from error


def main() -> None:
    faulthandler.register(signal.SIGUSR1, all_threads=True)  # `kill -USR1 <pid>` prints where every thread waits, no debugger needed
    if sys.argv[1:2] == [CHILD_FLAG]:
        child_main(*map(pathlib.Path, sys.argv[2:5]))
        return
    port.require_font()
    args = [arg for arg in sys.argv[1:] if arg != "--publish"]
    publishing = "--publish" in sys.argv[1:]
    if publishing and not shutil.which("git-filter-repo"):
        raise SystemExit("git-filter-repo not found")
    if publishing and not gate.homr_command():  # the gate needs it for every song; refuse now rather than after the builds
        raise SystemExit("homr not found: install it or set MUSE_HOMR")
    src_dir, out_dir, work = map(pathlib.Path, args[:3])
    wanted = args[3:]
    sources = ordered([src for src in song_sources(src_dir) if not wanted or src.stem.split("-")[0] in wanted])
    stage = work / "stage" if publishing else out_dir  # songs build apart from the repo that publishing rewrites
    began = time.monotonic()
    reports = []
    refused = []
    blocked = []
    workers = free_workers(len(sources))
    os.environ[launch.SLOTS_VARIABLE] = str(max(1, (os.cpu_count() or 1) // workers))  # each song child's share of the cores, read as it starts
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:  # threads only wait on children: no queue crosses a process
        jobs = {pool.submit(child, src, stage, work): src for src in sources}
        for job in concurrent.futures.as_completed(jobs):
            src = jobs[job]
            report, reason = job.result()
            if report is None:  # 1 song's failure never stops the others
                refused.append(src.stem)
                print(f"refused {src.stem}: {reason}", flush=True)
                continue
            reports.append(report)
            print(json.dumps(report, ensure_ascii=False), flush=True)
            if publishing:  # this process alone publishes, so songs publish 1 at a time as they finish
                reason = publish_song(src_dir, out_dir, work, stage, song_of(src).name)
                print(f"{'blocked' if reason else 'published'} {song_of(src).name} {reason}".rstrip(), flush=True)
                if reason:
                    blocked.append(song_of(src).name)
    previous = work / "build-report.json"
    earlier = json.loads(previous.read_text(encoding="utf-8")) if previous.exists() else {}
    earlier.update({str(r["song"]): r for r in reports})
    previous.write_text(json.dumps(earlier, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"songs {len(reports)} launches {sum(int(str(r['launches'])) for r in reports)} seconds {time.monotonic() - began:.1f}")
    if refused or blocked:
        raise SystemExit(f"refused: {refused} blocked: {blocked}")


if __name__ == "__main__":
    main()
