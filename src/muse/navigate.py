"""Compare MuseScore's unrolled playing order with the order the source's signs imply."""
from __future__ import annotations

import pathlib
import sys
import xml.etree.ElementTree as ET

from muse import launch
from muse.model import Bar, Score
from muse.song import decode, song_of, sources
from muse.verify import midi


def expected_order(score: Score) -> list[int]:
    """Walk bar numbers by the signs alone: repeats, voltas, then D.S. and To Coda without repeats."""
    bars = score.bars
    by_number: dict[int, Bar] = {b.number: b for b in bars}
    order: list[int] = []
    i = 1
    repeat_from = 1
    passed: set[int] = set()
    after_jump = False
    coda = next((b.number for b in bars if b.coda), None)
    segno = next((b.number for b in bars if b.segno), None)
    guard = 0
    while i <= len(bars):
        guard += 1
        if guard > 5000:
            raise RuntimeError("no end to the walk")
        bar = by_number[i]
        if bar.repeat_start:
            repeat_from = i
        second_time = after_jump or repeat_from in passed
        if bar.volta == "1" and second_time:
            while not by_number[i].repeat_end:
                i += 1
            i += 1
            continue
        order.append(i)
        if after_jump and bar.to_coda and coda:
            i = coda
            after_jump = False  # a repeat printed inside the coda still plays
            continue
        if bar.repeat_end and not after_jump and repeat_from not in passed:
            passed.add(repeat_from)
            i = repeat_from
            continue
        if bar.dal_segno and not after_jump and segno:
            after_jump = True
            i = segno
            continue
        i += 1
    return order


def signature(bar: Bar, semis: int) -> list[int]:
    return sorted(midi(h.step, h.alter, h.octave) + semis for voices in (bar.voices, bar.voices2) for v in voices for e in v for h in e.heads)


def check(src: pathlib.Path, out: pathlib.Path, work: pathlib.Path) -> tuple[int, int, list[str]]:
    info = song_of(src)
    song, semis = info.name, 0
    final = out / info.score_file
    mscx = work / f"{song}-unrolled.mscx"
    launch.mscore(["--unroll-repeats", "-o", str(mscx), str(final)], 240, check=True)
    root = ET.parse(mscx).getroot()
    staves = [st for st in root.iter("Staff") if st.find("Measure") is not None]
    played: list[list[int]] = []
    for row in zip(*(st.findall("Measure") for st in staves), strict=False):
        played.append(sorted(int(p.text or 0) for m in row for n in m.iter("Note") for p in n.findall("pitch")))
    score = decode(src)
    by_number = {b.number: b for b in score.bars}
    order = expected_order(score)
    problems = []
    if len(order) != len(played):
        problems.append(f"played {len(played)} bars, signs imply {len(order)}")
    for pos, (number, got) in enumerate(zip(order, played, strict=False)):
        if signature(by_number[number], semis) != got:
            problems.append(f"position {pos + 1}: signs imply bar {number}, played content differs")
            break
    return len(order), len(played), problems


def main() -> None:
    src_dir, out, work = map(pathlib.Path, sys.argv[1:4])
    bad = 0
    for src in sources(src_dir):
        implied, played, problems = check(src, out, work)
        print(f"{src.stem}: implied {implied} played {played} problems {len(problems)}")
        for p in problems:
            print("  ", p)
        bad += len(problems)
    raise SystemExit(1 if bad else 0)


if __name__ == "__main__":
    main()
