"""The 1 command a finished set must pass: run every check over every song and state every failure at once."""

from __future__ import annotations

import difflib
import hashlib
import itertools
import os
import pathlib
import shutil
import sys
import time
import xml.etree.ElementTree as ET
from functools import partial
from typing import TYPE_CHECKING

import pymupdf

from muse import (
    buildcache,
    chordnames,
    chordseat,
    fullcheck,
    inverse,
    launch,
    mode,
    navigate,
    pdfcheck,
    piano,
    port,
    scorefile,
    verify,
)
from muse.song import Song, decode, song_of, sources

if TYPE_CHECKING:
    from collections.abc import Callable

    from muse.model import Score

HOMR_SECONDS = 1500
MIN_HOMR_AGREEMENT = 0.6


def line_starts(pdf: pathlib.Path) -> list[int]:
    return sorted(set(port.system_starts(pdf)) | {1})


def line_lengths(starts: list[int], bars: int) -> list[int]:
    return [end - start for start, end in zip(starts, [*starts[1:], bars + 1], strict=True)]


def musicxml_letters(path: pathlib.Path) -> list[str]:
    """Note letters of a MusicXML file in file order, a chord counted by its last note, grace notes skipped.

    Returns:
        The note letters in file order.

    """
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
    """Share of our notes the other reading agrees with, and the bars where they part.

    Returns:
        The agreeing share and the sorted bars where the readings part.

    """
    matcher = difflib.SequenceMatcher(None, ours, theirs, autojunk=False)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    bars = {where[min(a, len(where) - 1)] for tag, a, _, _, _ in matcher.get_opcodes() if tag != "equal" and where}
    return (matched / len(ours) if ours else 1.0), sorted(bars)


def homr_command() -> str | None:
    return os.environ.get("MUSE_HOMR") or shutil.which("homr")


def homr_reading(src: pathlib.Path, page_no: int, work: pathlib.Path, command: str) -> pathlib.Path:
    """Homr's reading of 1 source page, cached by the source's own bytes.

    Returns:
        The path of the MusicXML reading.

    """
    digest = hashlib.sha256(src.read_bytes()).hexdigest()[:16]
    folder = work / "homr" / digest
    folder.mkdir(parents=True, exist_ok=True)
    image = folder / f"page{page_no + 1}.png"
    result = image.with_suffix(".musicxml")
    if not result.exists():
        pymupdf.open(src)[page_no].get_pixmap(dpi=300).save(image)
        launch.heavy([command, image.name], HOMR_SECONDS, check=True, cwd=folder)
    return result


def homr_findings(src: pathlib.Path, score: Score, work: pathlib.Path) -> tuple[list[str], str]:
    command = homr_command()
    if command is None:
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


def folder_problems(out: pathlib.Path, songs: list[Song]) -> list[str]:
    """Check that the sheets repo holds exactly the songs' folders, and each folder exactly its files.

    Returns:
        Every missing and every stray file.

    """
    wanted: set[str] = set()
    for song in songs:
        wanted |= song.files()
    held = {
        str(p.relative_to(out))
        for p in out.rglob("*")
        if p.is_file() and ".git" not in p.relative_to(out).parts and p.name != ".DS_Store"
    }
    return [f"missing {name}" for name in sorted(wanted - held)] + [f"stray {name}" for name in sorted(held - wanted)]


def piano_problems(src: pathlib.Path, sheet: pathlib.Path) -> list[str]:
    """1 staff, notes only in the kept parts, every other rest hidden, every chord present.

    Returns:
        Every way the piano sheet departs from that.

    """
    original = decode(src)
    parts = piano.kept_parts(original)
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
    return problems


def _guarded(failures: list[str], label: str, step: Callable[[], list[str]]) -> None:
    try:
        failures.extend(f"{label}: {problem}" for problem in step())
    except launch.MachineUnavailableError:
        raise
    except (
        port.DoesNotFitError,
        RuntimeError,
        OSError,
        TypeError,
        ValueError,
        AssertionError,
        LookupError,
        AttributeError,
    ) as error:
        failures.append(f"{label}: could not run: {str(error)[:160]}")


type Guard = Callable[[str, Callable[[], list[str]]], None]


def _score_file_checks(guarded: Guard, src: pathlib.Path, out: pathlib.Path, work: pathlib.Path, song: Song) -> None:
    guarded("verify", lambda: verify.check(src, out, work))
    guarded("navigate", lambda: navigate.check(src, out, work)[2])
    guarded("tone", lambda: inverse.check(src, out, work))
    guarded("chord beats", lambda: verify.chord_beat_problems(src, out / song.score_file))
    guarded("signs built in", lambda: scorefile.builtin_sign_problems(out / song.score_file))
    guarded("empty texts", lambda: scorefile.empty_text_problems(out / song.score_file))
    guarded("chord file", lambda: chordnames.chord_path_problems(out / song.score_file))


def _judge_pdf(guarded: Guard, notes: list[str], final: pathlib.Path, song: Song, wanted: int) -> None:
    short = final.stem.rsplit("-", 1)[-1]

    def signs() -> list[str]:
        printed = len(pdfcheck.sign_sizes(final)[0])
        return [f"{wanted} signs in the score, {printed} printed"] if printed < wanted else []

    guarded(f"pdf {short}", lambda: pdfcheck.all_problems(final, song.title))
    guarded(f"words {short}", lambda: verify.pdf_words_problems(final, song.title))
    guarded(f"pages {short}", lambda: port.balance_problems(final, line_starts(final)))
    guarded(f"signs {short}", signs)
    notes.append(f"{final.name}: {len(pymupdf.open(final))} pages")


def _ties(notes: list[str], out: pathlib.Path, song: Song, score: Score) -> list[str]:
    wrong: list[str] = []
    for final in sorted({out / song.original_pdf, out / song.tone_pdf}):
        starts = line_starts(final)
        bad, long_runs = port.tie_findings(starts, [port.tied_out(bar) for bar in score.bars])
        lengths = line_lengths(starts, len(score.bars))
        short = sum(1 for length in lengths if length <= port.SHORT_LINE_BARS)
        notes.append(f"{final.name}: {short} of {len(lengths)} lines hold 2 bars or fewer")
        wrong += port.short_line_problems([
            list(range(first, first + length)) for first, length in zip(starts, lengths, strict=True)
        ])
        notes.extend(long_runs)
        wrong += bad
    return wrong


def _even_bar_checks(guarded: Guard, src: pathlib.Path, out: pathlib.Path, work: pathlib.Path, song: Song) -> None:
    sheet = work / f"{song.name}-piano-final.mscz"
    guarded("piano", lambda: piano_problems(src, sheet))
    guarded("even bars piano", lambda: even_bars(src, sheet, out / song.piano_pdf, work, reduced=True))
    guarded(
        "even bars original",
        lambda: even_bars(src, out / song.score_file, out / song.original_pdf, work, reduced=False),
    )
    if song.direction:
        guarded(
            "even bars tone",
            lambda: even_bars(src, work / f"{song.name}-{port.TONE}.mscz", out / song.tone_pdf, work, reduced=False),
        )


def _line_checks(guarded: Guard, out: pathlib.Path, work: pathlib.Path, song: Song, score: Score) -> None:
    sheet = work / f"{song.name}-piano-final.mscz"
    guarded("line start piano", lambda: port.chord_starts(sheet, score))
    guarded("line start original", lambda: port.chord_starts(out / song.score_file, score))
    if song.direction:
        guarded("line start tone", lambda: port.chord_starts(work / f"{song.name}-{port.TONE}.mscz", score))
    guarded("chord seat piano", lambda: chordseat.problems(sheet, measured(out / song.piano_pdf, work)))
    guarded(
        "chord seat original",
        lambda: chordseat.problems(out / song.score_file, measured(out / song.original_pdf, work)),
    )
    if song.direction:
        guarded(
            "chord seat tone",
            lambda: chordseat.problems(work / f"{song.name}-{port.TONE}.mscz", measured(out / song.tone_pdf, work)),
        )


def _homr(notes: list[str], src: pathlib.Path, score: Score, work: pathlib.Path) -> list[str]:
    wrong, note = homr_findings(src, score, work)
    if note:
        notes.append(note)
    return wrong


def check_song(src: pathlib.Path, out: pathlib.Path, work: pathlib.Path) -> tuple[list[str], list[str]]:
    failures: list[str] = []
    notes: list[str] = []
    guarded = partial(_guarded, failures)
    info = song_of(src)
    if info.orchestral:
        return fullcheck.problems(src, out, work, info)
    score = decode(src)
    song = song_of(src, score)
    _score_file_checks(guarded, src, out, work, song)
    wanted = sum(bar.segno or bar.coda for bar in score.bars)
    for final in sorted({out / song.original_pdf, out / song.tone_pdf, out / song.piano_pdf}):
        guarded(f"bass {final.name}", partial(pdfcheck.bass_problems, final))
        _judge_pdf(guarded, notes, final, song, wanted)
    guarded("lines", partial(_ties, notes, out, song, score))
    _even_bar_checks(guarded, src, out, work, song)
    port.transpose_options(song)
    _line_checks(guarded, out, work, song, score)
    guarded("homr", partial(_homr, notes, src, score, work))
    return failures, notes


def measured(pdf: pathlib.Path, work: pathlib.Path) -> pathlib.Path:
    """Copy a final PDF beside the program's measure positions for it, outside the sheets folder.

    Returns:
        The path of the copy.

    Raises:
        DoesNotFitError: No render that drew the PDF left measure positions for it.

    """
    folder = work / "measured"
    folder.mkdir(parents=True, exist_ok=True)
    copy = folder / pdf.name
    shutil.copyfile(pdf, copy)
    target = copy.with_suffix(".mpos")
    drawn = port.paired(work, pdf.name)
    if not port.pairs(drawn, pdf):
        msg = f"{pdf.name}: no measure positions from the render that drew it"
        raise port.DoesNotFitError(msg)
    shutil.copyfile(drawn, target)
    return copy


def even_bars(
    src: pathlib.Path,
    score_path: pathlib.Path,
    pdf: pathlib.Path,
    work: pathlib.Path,
    *,
    reduced: bool,
) -> list[str]:
    """Empty bars of 1 line are equal on every sheet: chord text never sets a width.

    Returns:
        Every uneven-bar problem.

    """
    score = decode(src)
    if reduced:
        piano.reduce(score)
    probe = measured(pdf, work)
    widened = (
        ({bar.number for bar in score.bars if len(bar.chords) > 1} | chord_bound_bars(score_path, probe))
        if mode.PRINT
        else set()
    )
    return chordseat.uneven(score_path, probe, {bar.number for bar in score.bars if piano.has_notes(bar)} | widened)


BARLINE_LEAD = 2.0


def chord_bound_bars(score_path: pathlib.Path, pdf: pathlib.Path) -> set[int]:
    """Find 1-based bars whose chords, never the line, set their width.

    A bar is found when its chords leave a gap within the even-bars tolerance of the clear chord gap,
    between 2 of their own chords or before the next bar's first.

    Returns:
        The 1-based numbers of those bars.

    """
    text = chordseat.score_text(score_path)[1]
    rows = [row for row, chords in zip(chordseat.bar_rows(text), chordseat.rows_of(text), strict=True) if chords]
    found = set()
    lines: list[chordseat.LineRecord] = chordseat.matched(text, pdf)
    for row, line in zip(rows, lines, strict=False):
        edges = [line["opening"], *line["bars"]]
        if len(edges) - 1 != len(row):
            continue
        boxes = sorted(zip(line["lefts"], line["rights"], strict=True))
        clear = chordseat.CHORD_GAP * line["size"]
        for at, bar in enumerate(row[:-1]):
            mine = [box for box in boxes if edges[at] - BARLINE_LEAD <= box[0] < edges[at + 1] - BARLINE_LEAD]
            after = [box for box in boxes if edges[at + 1] - BARLINE_LEAD <= box[0] < edges[at + 2] - BARLINE_LEAD][:1]
            chain = mine + after
            gaps = [nxt[0] - box[1] for box, nxt in itertools.pairwise(chain)]
            if mine and gaps and min(gaps) <= clear + chordseat.EQUAL_TOLERANCE:
                found.add(bar)
    return found


def gate_key(src: pathlib.Path) -> str:
    """Build the key a gate pass is recorded under: the source, the code and the private lists it judges against.

    Returns:
        The cache key.

    """
    private = pathlib.Path(os.environ["MUSE_PRIVATE"])
    lists = "".join(
        buildcache.file_digest(path)
        for path in sorted(private.glob("*.txt")) + sorted(private.glob("*.json"))
        if path.is_file()
    )
    return buildcache.key_of(src, repr(song_of(src)), buildcache.code_files(), lists)


def cached_check(src: pathlib.Path, out: pathlib.Path, work: pathlib.Path) -> tuple[list[str], list[str], bool]:
    """Check 1 song, reusing a recorded pass.

    A pass stays valid while the source, code, style, private lists and every checked file are unchanged;
    a failure is always re-run.

    Returns:
        The failures, the notes, and whether the recorded pass was reused.

    """
    info = song_of(src)
    outputs = sorted(path for path in (out / info.name).iterdir() if path.is_file()) + (
        [] if info.orchestral else [work / f"{info.name}-piano-final.mscz"]
    )
    key = gate_key(src)
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


def chord_name_problems(work: pathlib.Path) -> list[str]:
    """Every short spelling plays as proved, and transposes, with the sheets' own style.

    Returns:
        Every playback and transposition problem.

    """
    style = port.styled(work, "chord-check.mss", "x", port.Sizing(1.5, 11.0))
    program = shutil.which("mscore") or "mscore"
    return [
        *chordnames.playback_problems(program, style, work, run=chordnames.checked_run),
        *chordnames.transposes(program, style, work, chordnames.checked_run),
    ]


def verdicts(
    src_dir: pathlib.Path,
    out: pathlib.Path,
    work: pathlib.Path,
    names: tuple[str, ...] = (),
) -> dict[str, list[str]]:
    """Each song folder's own failures, its files included.

    Returns:
        The failures of each song folder, by song name.

    """
    work.mkdir(parents=True, exist_ok=True)
    found: dict[str, list[str]] = {}
    chord_trouble = chord_name_problems(work)
    for src in sources(src_dir):
        info = song_of(src)
        if names and info.name not in names:
            continue
        files = info.files()
        folder = out / info.name
        held = (
            {f"{info.name}/{p.name}" for p in folder.iterdir() if p.is_file() and p.name != ".DS_Store"}
            if folder.exists()
            else set()
        )
        found[info.name] = [f"missing {name}" for name in sorted(files - held)] + [
            f"stray {name}" for name in sorted(held - files)
        ]
        if not found[info.name]:
            try:
                found[info.name] = chord_trouble + cached_check(src, out, work)[0]
            except (
                port.DoesNotFitError,
                RuntimeError,
                OSError,
                TypeError,
                ValueError,
                AssertionError,
                LookupError,
                AttributeError,
            ) as error:
                found[info.name] = [f"refused: {error}"]
        sys.stdout.write(f"{info.name}: {len(found[info.name])} failures\n")
        sys.stdout.flush()
    return found


def main() -> None:
    src_dir, out, work = map(pathlib.Path, sys.argv[1:4])
    work.mkdir(parents=True, exist_ok=True)
    total = 0
    wanted = sys.argv[4:]
    found = [src for src in sources(src_dir) if not wanted or src.stem.split("-")[0] in wanted]
    trouble = folder_problems(out, [song_of(src) for src in sources(src_dir)])
    for problem in trouble:
        sys.stdout.write(f"  FAIL: folders: {problem}\n")
    total += len(trouble)
    for src in found:
        began = time.monotonic()
        try:
            failures, notes, cached = cached_check(src, out, work)
        except (
            port.DoesNotFitError,
            RuntimeError,
            OSError,
            TypeError,
            ValueError,
            AssertionError,
            LookupError,
            AttributeError,
        ) as error:
            failures, notes, cached = [f"refused: {error}"], [], False
        elapsed = time.monotonic() - began
        sys.stdout.write(f"{src.stem}: {len(failures)} failures {elapsed:.1f}s{' cached' if cached else ''}\n")
        for note in notes:
            sys.stdout.write(f"  note: {note}\n")
        for failure in failures:
            sys.stdout.write(f"  FAIL: {failure}\n")
        total += len(failures)
    sys.stdout.write(f"failures {total}\n")
    raise SystemExit(1 if total else 0)


if __name__ == "__main__":
    main()
