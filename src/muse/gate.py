"""The 1 command a finished set must pass: run every check over every song and state every failure at once."""
from __future__ import annotations

import difflib
import hashlib
import os
import pathlib
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Callable

import pymupdf

from muse import inverse, navigate, pdfcheck, port, verify
from muse.model import Score
from muse.song import decode, song_of, sources

A4 = (595.0, 842.0)
A4_TOLERANCE = 1.5
EDGE = 8.0  # points of clear paper on every side
MIN_TIED_CHAIN = 4  # a tied run of this many bars may not fit 1 line
MIN_HOMR_AGREEMENT = 0.6  # below this the independent reading has stopped agreeing about the song
LOW_DPI = 72


def page_problems(path: pathlib.Path) -> list[str]:
    problems = []
    doc = pymupdf.open(path)
    if len(doc) > port.MAX_PAGES:
        problems.append(f"{len(doc)} pages, limit {port.MAX_PAGES}")
    for number, page in enumerate(doc, 1):
        if abs(page.rect.width - A4[0]) > A4_TOLERANCE or abs(page.rect.height - A4[1]) > A4_TOLERANCE:
            problems.append(f"p{number}: not A4 ({page.rect.width:.0f} x {page.rect.height:.0f})")
    return problems


def ink_problems(path: pathlib.Path) -> list[str]:
    problems = []
    for number, page in enumerate(pymupdf.open(path), 1):
        pix = page.get_pixmap(dpi=LOW_DPI, colorspace=pymupdf.csGRAY)
        width, height, data = pix.width, pix.height, pix.samples
        rows = [y for y in range(height) if min(data[y * width:(y + 1) * width]) < 128]
        cols = [x for x in range(width) if min(data[x::width][:height]) < 128]
        if not rows:
            problems.append(f"p{number}: blank page")
        elif rows[0] < EDGE or rows[-1] > height - EDGE or cols[0] < EDGE or cols[-1] > width - EDGE:
            problems.append(f"p{number}: ink within {EDGE:g} points of the edge")
    return problems


def tie_findings(starts: list[int], tied: list[bool]) -> tuple[list[str], list[str]]:
    """A tie over a line end fails unless its tied run is too long for 1 line, which is only reported."""
    failures: list[str] = []
    reported: list[str] = []
    for start in starts:
        if start < 2 or start > len(tied) or not tied[start - 2]:
            continue
        low = start - 2
        while low > 0 and tied[low - 1]:
            low -= 1
        high = start - 2
        while high + 1 < len(tied) and tied[high + 1]:
            high += 1
        run = high - low + 2
        (reported if run >= MIN_TIED_CHAIN else failures).append(f"tie over line end into bar {start} (tied run of {run} bars)")
    return failures, reported


def line_starts(pdf: pathlib.Path) -> list[int]:
    return sorted(set(port.system_starts(pdf)) | {1})


def line_lengths(starts: list[int], bars: int) -> list[int]:
    return [end - start for start, end in zip(starts, [*starts[1:], bars + 1], strict=True)]


def musicxml_letters(path: pathlib.Path) -> list[str]:
    """Note letters of a MusicXML file in file order, a chord counted by its last note, grace notes skipped."""
    letters: list[str] = []
    for note in ET.parse(path).getroot().iter("note"):
        step = note.findtext("pitch/step")
        if step is None or note.find("grace") is not None:
            continue
        if note.find("chord") is not None and letters:
            letters[-1] = step
        else:
            letters.append(step)
    return letters


def agreement(ours: list[str], where: list[int], theirs: list[str]) -> tuple[float, list[int]]:
    """Share of our notes the other reading agrees with, and the bars where they part."""
    matcher = difflib.SequenceMatcher(None, ours, theirs, autojunk=False)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    bars = {where[min(a, len(where) - 1)] for tag, a, _, _, _ in matcher.get_opcodes() if tag != "equal" and where}
    return (matched / len(ours) if ours else 1.0), sorted(bars)


def homr_command() -> str | None:
    return os.environ.get("MUSE_HOMR") or shutil.which("homr")


def homr_reading(src: pathlib.Path, page_no: int, work: pathlib.Path, command: str) -> pathlib.Path:
    """homr's reading of 1 source page, cached by the source's own bytes."""
    digest = hashlib.sha256(src.read_bytes()).hexdigest()[:16]
    folder = work / "homr" / digest
    folder.mkdir(parents=True, exist_ok=True)
    image = folder / f"page{page_no + 1}.png"
    result = image.with_suffix(".musicxml")
    if not result.exists():
        pymupdf.open(src)[page_no].get_pixmap(dpi=300).save(image)
        subprocess.run([command, image.name], cwd=folder, capture_output=True, check=True, timeout=1500, stdin=subprocess.DEVNULL)
    return result


def homr_findings(src: pathlib.Path, score: Score, work: pathlib.Path) -> tuple[list[str], str]:
    command = homr_command()
    if not command:
        return ["homr not found: install it or set MUSE_HOMR"], ""
    ours: list[str] = []
    where: list[int] = []
    theirs: list[str] = []
    for page_no in sorted({bar.page for bar in score.bars}):
        for bar in (b for b in score.bars if b.page == page_no):
            for voices in (bar.voices, bar.voices2):
                for ev in (e for voice in voices for e in voice if e.heads and not e.grace):
                    ours.append(ev.heads[-1].step)
                    where.append(bar.number)
        theirs += musicxml_letters(homr_reading(src, page_no, work, command))
    share, bars = agreement(ours, where, theirs)
    failures = [f"homr agrees on only {share:.0%} of notes"] if share < MIN_HOMR_AGREEMENT else []
    return failures, f"homr agrees on {share:.0%}; differs near bars {bars}"


def check_song(src: pathlib.Path, out: pathlib.Path, work: pathlib.Path) -> tuple[list[str], list[str]]:
    failures: list[str] = []
    notes: list[str] = []

    def guarded(label: str, step: Callable[[], list[str]]) -> None:
        try:
            failures.extend(f"{label}: {problem}" for problem in step())
        except Exception as error:  # every check must speak even when another crashed
            failures.append(f"{label}: could not run: {str(error)[:160]}")

    score = decode(src)
    song = song_of(src, score)
    final = out / f"{song.name}-{song.tone}.pdf"
    guarded("verify", lambda: verify.check(src, out, work))
    guarded("navigate", lambda: navigate.check(src, out, work)[2])
    guarded("inverse", lambda: inverse.check(src, out, work))
    guarded("pdf", lambda: pdfcheck.all_problems(final, song.title))
    guarded("pages", lambda: page_problems(final))
    wanted = sum(bar.segno or bar.coda for bar in score.bars)
    guarded("signs", lambda: [f"{wanted} signs in the score, {len(pdfcheck.sign_sizes(final)[0])} printed"] if len(pdfcheck.sign_sizes(final)[0]) < wanted else [])
    guarded("ink", lambda: ink_problems(final))

    def ties() -> list[str]:
        starts = line_starts(final)
        wrong, long_runs = tie_findings(starts, [port.tied_out(bar) for bar in score.bars])
        lengths = line_lengths(starts, len(score.bars))
        short = sum(1 for length in lengths if length <= 2)
        notes.append(f"{short} of {len(lengths)} lines hold 2 bars or fewer")
        notes.extend(long_runs)
        return wrong

    guarded("lines", ties)

    def homr() -> list[str]:
        wrong, note = homr_findings(src, score, work)
        if note:
            notes.append(note)
        return wrong

    guarded("homr", homr)
    return failures, notes


def main() -> None:
    src_dir, out, work = map(pathlib.Path, sys.argv[1:4])
    work.mkdir(parents=True, exist_ok=True)
    total = 0
    for src in sources(src_dir):
        try:
            failures, notes = check_song(src, out, work)
        except Exception as error:  # a refused source is a failure of this song, never the end of the run
            failures, notes = [f"refused: {error}"], []
        print(f"{src.stem}: {len(failures)} failures")
        for note in notes:
            print("  note:", note)
        for failure in failures:
            print("  FAIL:", failure)
        total += len(failures)
    print("failures", total)
    raise SystemExit(1 if total else 0)


if __name__ == "__main__":
    main()
