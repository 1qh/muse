"""Compare MuseScore's unrolled playing order with the order the source's signs imply."""

from __future__ import annotations

import pathlib
import sys
import xml.etree.ElementTree as ET
from typing import TYPE_CHECKING

from muse import launch
from muse.song import decode, song_of, sources
from muse.verify import midi

if TYPE_CHECKING:
    from muse.model import Bar, Score

WALK_STEP_LIMIT = 5000


def expected_order(score: Score) -> list[int]:
    """Walk bar numbers by the signs alone: repeats, voltas, then D.S. and To Coda without repeats.

    Returns:
        The bar numbers in playing order.

    Raises:
        RuntimeError: the walk never reaches the last bar.
    """
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
        if guard > WALK_STEP_LIMIT:
            msg = "no end to the walk"
            raise RuntimeError(msg)
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
        if after_jump and bar.to_coda and coda is not None and coda != 0:
            i = coda
            after_jump = False
            continue
        if bar.repeat_end and not after_jump and repeat_from not in passed:
            passed.add(repeat_from)
            i = repeat_from
            continue
        if bar.dal_segno and not after_jump and segno is not None and segno != 0:
            after_jump = True
            i = segno
            continue
        i += 1
    return order


def signature(bar: Bar, semis: int) -> list[int]:
    return sorted(
        midi(h.step, h.alter, h.octave) + semis
        for voices in (bar.voices, bar.voices2)
        for v in voices
        for e in v
        for h in e.heads
    )


def check(src: pathlib.Path, out: pathlib.Path, work: pathlib.Path) -> tuple[int, int, list[str]]:
    info = song_of(src)
    song, semis = info.name, 0
    final = out / info.score_file
    mscx = work / f"{song}-unrolled.mscx"
    launch.mscore(["--unroll-repeats", "-o", str(mscx), str(final)], 240, check=True)
    root = ET.parse(mscx).getroot()
    staves = [st for st in root.iter("Staff") if st.find("Measure") is not None]
    played: list[list[int]] = [
        sorted(int(p.text or 0) for m in row for n in m.iter("Note") for p in n.findall("pitch"))
        for row in zip(*(st.findall("Measure") for st in staves), strict=False)
    ]
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
        sys.stdout.write(f"{src.stem}: implied {implied} played {played} problems {len(problems)}\n")
        for p in problems:
            sys.stdout.write("   " + p + "\n")
        bad += len(problems)
    raise SystemExit(1 if bad else 0)


if __name__ == "__main__":
    main()
