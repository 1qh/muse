"""The 1 command a finished set must pass: run every check over every song and state every failure at once."""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable

import pymupdf

from muse import buildcache, inverse, navigate, pdfcheck, piano, port, scorefile, verify
from muse.model import Score
from muse.song import Song, decode, song_of, sources

A4 = (595.0, 842.0)
A4_TOLERANCE = 1.5
EDGE = 8.0  # points of clear paper on every side
MIN_TIED_CHAIN = 4  # a tied run of this many bars may not fit 1 line
MIN_HOMR_AGREEMENT = 0.6  # below this the independent reading has stopped agreeing about the song
LOW_DPI = 72
WITHHELD = "withheld.json"


def legacy_staff_problems(legacy: pathlib.Path, work: pathlib.Path, title: str) -> list[str]:
    """Render the file in MuseScore 3 itself and refuse an empty staff on a shared line."""
    pdf = work / f"{legacy.stem}-staves.pdf"
    subprocess.run([port.legacy_command(), "-o", str(pdf), str(legacy)], capture_output=True, check=True, timeout=240, stdin=subprocess.DEVNULL)
    return pdfcheck.empty_staves(pdf) + [f"text on text: {c[2]}" for c in pdfcheck.text_collisions(pdf)] + pdfcheck.title_problems(pdf, title)


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


def folder_problems(out: pathlib.Path, songs: list[Song], withheld: set[str]) -> list[str]:
    """The sheets repo holds exactly the songs' folders, and each folder exactly its files."""
    wanted: set[str] = set()
    for song in songs:
        wanted |= {name for name in song.files() if name != song.legacy_file or song.name not in withheld}
    held = {str(p.relative_to(out)) for p in out.rglob("*") if p.is_file() and ".git" not in p.relative_to(out).parts and p.name != ".DS_Store"}
    return [f"missing {name}" for name in sorted(wanted - held)] + [f"stray {name}" for name in sorted(held - wanted)]


def withheld_songs() -> dict[str, str]:
    """Songs whose MuseScore 3 file is held back, each with its stated reason."""
    path = pathlib.Path(os.environ["MUSE_PRIVATE"]) / WITHHELD
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def piano_problems(src: pathlib.Path, sheet: pathlib.Path) -> list[str]:
    """1 staff, notes only in the kept parts, every other rest hidden, every chord present."""
    original = decode(src)
    parts = piano.parts(original)
    kept = {n for part in parts for n in range(part.first, part.last + 1)}
    root = scorefile.root_of(sheet)
    staves = scorefile.staves(root)
    problems = [f"{len(staves)} staves"] if len(staves) != 1 else []
    bars = scorefile.read(sheet)[0]
    if len(bars) != len(original.bars):
        problems.append(f"{len(bars)} bars, source has {len(original.bars)}")
    for number, (held, measure, bar) in enumerate(zip(bars, staves[0].findall("Measure"), original.bars, strict=False), 1):
        if held.pitches and number not in kept:
            problems.append(f"bar {number}: notes outside the kept parts")
        if number not in kept and any(rest.findtext("visible") != "0" for rest in measure.iter("Rest")):
            problems.append(f"bar {number}: rest not hidden")
        if len(held.chords) != len(bar.chords):
            problems.append(f"bar {number}: {len(held.chords)} chords, source has {len(bar.chords)}")
    starts = [1] + [number + 1 for number, measure in enumerate(staves[0].findall("Measure"), 1) if measure.find("LayoutBreak") is not None and number < len(bars)]
    reduced = decode(src)
    piano.reduce(reduced)
    noted = {bar.number for bar in reduced.bars if piano.has_notes(bar)}
    marks = {index + 1 for index, bar in enumerate(reduced.bars) if bar.rehearsal or bar.segno or bar.coda or (index and reduced.bars[index - 1].final)}
    problems += [f"bar {first}: lone bar on a line" for first in piano.lone_lines(starts, len(bars))]
    problems += [f"bar {first}: short empty line could merge" for first in piano.sparse_lines(starts, noted, marks, len(bars), {bar.number: piano.chord_letters(bar) for bar in reduced.bars})]
    return problems


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
    guarded("verify", lambda: verify.check(src, out, work))
    guarded("navigate", lambda: navigate.check(src, out, work)[2])
    guarded("tone", lambda: inverse.check(src, out, work))
    guarded("chord beats", lambda: verify.chord_beat_problems(src, out / song.score_file))
    guarded("signs built in", lambda: scorefile.builtin_sign_problems(out / song.score_file))
    legacy = out / song.legacy_file
    if legacy.exists():
        guarded("ms3", lambda: scorefile.difference(out / song.score_file, legacy))
        guarded("ms3 chord beats", lambda: verify.chord_beat_problems(src, legacy))
        guarded("ms3 signs built in", lambda: scorefile.builtin_sign_problems(legacy))
        guarded("ms3 staves", lambda: legacy_staff_problems(legacy, work, song.title))
    wanted = sum(bar.segno or bar.coda for bar in score.bars)

    def judge_pdf(final: pathlib.Path) -> None:
        short = final.stem.rsplit("-", 1)[-1]

        def signs() -> list[str]:
            printed = len(pdfcheck.sign_sizes(final)[0])
            return [f"{wanted} signs in the score, {printed} printed"] if printed < wanted else []

        guarded(f"pdf {short}", lambda: pdfcheck.all_problems(final, song.title))
        guarded(f"words {short}", lambda: verify.pdf_words_problems(final, song.title))
        guarded(f"pages {short}", lambda: page_problems(final))
        guarded(f"ink {short}", lambda: ink_problems(final))
        guarded(f"signs {short}", signs)
        notes.append(f"{final.name}: {len(pymupdf.open(final))} pages")

    for final in sorted({out / song.original_pdf, out / song.tone_pdf, out / song.piano_pdf}):
        judge_pdf(final)

    def ties() -> list[str]:
        wrong: list[str] = []
        for final in sorted({out / song.original_pdf, out / song.tone_pdf}):
            starts = line_starts(final)
            bad, long_runs = tie_findings(starts, [port.tied_out(bar) for bar in score.bars])
            lengths = line_lengths(starts, len(score.bars))
            short = sum(1 for length in lengths if length <= 2)
            notes.append(f"{final.name}: {short} of {len(lengths)} lines hold 2 bars or fewer")
            notes.extend(long_runs)
            wrong += bad
        return wrong

    guarded("lines", ties)
    sheet = work / f"{song.name}-piano-final.mscz"
    guarded("piano", lambda: piano_problems(src, sheet))

    def homr() -> list[str]:
        wrong, note = homr_findings(src, score, work)
        if note:
            notes.append(note)
        return wrong

    guarded("homr", homr)
    return failures, notes


def cached_check(src: pathlib.Path, out: pathlib.Path, work: pathlib.Path) -> tuple[list[str], list[str], bool]:
    """A pass stays valid while the source, code, style, private lists and every checked file are unchanged; a failure is always re-run."""
    info = song_of(src)
    outputs = sorted(path for path in (out / info.name).iterdir() if path.is_file()) + [work / f"{info.name}-piano-final.mscz"]
    private = pathlib.Path(os.environ["MUSE_PRIVATE"])
    lists = "".join(buildcache.file_digest(path) for path in sorted(private.glob("*.txt")) + sorted(private.glob("*.json")) if path.is_file())
    key = buildcache.key_of(src, repr(info), buildcache.code_files(), lists)
    state = buildcache.load(info.name)
    if buildcache.fresh(state.get("gate"), key, outputs):
        return [], list(state["gate"]["notes"]), True
    failures, notes = check_song(src, out, work)
    state = buildcache.load(info.name)
    state.pop("gate", None)
    if not failures:
        state["gate"] = buildcache.record(key, outputs, notes=notes)
    buildcache.save(info.name, state)
    return failures, notes, False


def verdicts(src_dir: pathlib.Path, out: pathlib.Path, work: pathlib.Path, names: tuple[str, ...] = ()) -> dict[str, list[str]]:
    """Each song folder's own failures, its files included."""
    work.mkdir(parents=True, exist_ok=True)
    held_back = set(withheld_songs())
    found: dict[str, list[str]] = {}
    for src in sources(src_dir):
        info = song_of(src)
        if names and info.name not in names:
            continue
        files = {name for name in info.files() if name != info.legacy_file or info.name not in held_back}
        folder = out / info.name
        held = {f"{info.name}/{p.name}" for p in folder.iterdir() if p.is_file() and p.name != ".DS_Store"} if folder.exists() else set()
        found[info.name] = [f"missing {name}" for name in sorted(files - held)] + [f"stray {name}" for name in sorted(held - files)]
        if not found[info.name]:
            try:
                found[info.name] = cached_check(src, out, work)[0]
            except Exception as error:  # a refused source blocks its song, never the run
                found[info.name] = [f"refused: {error}"]
        print(f"{info.name}: {len(found[info.name])} failures", flush=True)
    return found


def main() -> None:
    src_dir, out, work = map(pathlib.Path, sys.argv[1:4])
    work.mkdir(parents=True, exist_ok=True)
    total = 0
    wanted = sys.argv[4:]
    found = [src for src in sources(src_dir) if not wanted or src.stem.split("-")[0] in wanted]
    held_back = withheld_songs()
    for name, reason in held_back.items():
        print(f"withheld MuseScore 3 file: {name}: {reason}")
    trouble = folder_problems(out, [song_of(src) for src in sources(src_dir)], set(held_back))
    for problem in trouble:
        print("  FAIL: folders:", problem)
    total += len(trouble)
    for src in found:
        began = time.monotonic()
        try:
            failures, notes, cached = cached_check(src, out, work)
        except Exception as error:  # a refused source is a failure of this song, never the end of the run
            failures, notes, cached = [f"refused: {error}"], [], False
        print(f"{src.stem}: {len(failures)} failures {time.monotonic() - began:.1f}s{' cached' if cached else ''}")
        for note in notes:
            print("  note:", note)
        for failure in failures:
            print("  FAIL:", failure)
        total += len(failures)
    print("failures", total)
    raise SystemExit(1 if total else 0)


if __name__ == "__main__":
    main()
