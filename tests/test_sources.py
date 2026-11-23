"""Decoders against the real sheets, kept outside the repo; MUSE_SOURCES must name them."""

import collections
import hashlib
import itertools
import json
import os
import pathlib
from fractions import Fraction
from typing import Any

import pymupdf
import pytest

from muse import fullcheck, fullscore, launch, leland, orchestral, song, symbolfont
from muse.leland import is_lyric
from muse.model import Score
from muse.song import decode, encoding, song_of


def pins() -> dict[str, Any]:
    path = os.environ.get("MUSE_PINS")
    if not path:
        pytest.fail("MUSE_PINS is not set")
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))


def names() -> list[str]:
    path = os.environ.get("MUSE_PINS")
    return (
        sorted(json.loads(pathlib.Path(path).read_text(encoding="utf-8"))["songs"]) if path else ["MUSE_PINS is not set"]
    )


def source(name: str) -> pathlib.Path:
    root = os.environ.get("MUSE_SOURCES")
    if not root:
        pytest.fail("MUSE_SOURCES is not set")
    for folder in root.split(os.pathsep):
        path = pathlib.Path(folder) / f"{name}.pdf"
        if path.exists():
            return path
    pytest.fail(f"{name} is in no source folder")


def reading(score: Score) -> str:
    """Render everything a decoder reads as a stable text form.

    Returns:
        The reading, 1 row per voice, chord list, flag set, text list and rehearsal list.
    """
    rows = []
    for b in score.bars:
        for voices in (b.voices, b.voices2):
            rows.extend(
                " ".join(("+".join(f"{h.step}{h.alter}{h.octave}" for h in e.heads) or "r") + f":{e.length}" for e in v)
                for v in voices
            )
        rows.extend((
            "|".join(f"{c.root}{c.root_alter}{c.kind}/{c.bass}{c.bass_alter}" for c in b.chords),
            f"{b.repeat_start}{b.repeat_end}{b.volta}{b.segno}{b.coda}{b.to_coda}{b.dal_segno}",
            "|".join(
                f"{'^' if text.above else 'v'}" + "".join(run[0] for run in text.runs) for text in [*b.texts, *b.texts2]
            ),
            "|".join(b.rehearsal),
        ))
    return "\n".join(rows)


@pytest.mark.parametrize("name", names())
def test_bar_count_and_no_flags(name: str) -> None:
    score = decode(source(name))
    assert len(score.bars) == pins()["songs"][name]["bars"]
    assert score.flags == []


@pytest.mark.parametrize("name", names())
def test_every_voice_fills_its_bar(name: str) -> None:
    for b in decode(source(name)).bars:
        for voices in (b.voices, b.voices2):
            for v in voices:
                assert sum((e.length for e in v), Fraction(0)) == b.length, b.number


def test_all_notes_of_a_beam_share_a_group() -> None:
    groups = [
        e.beam
        for b in decode(source(pins()["beam_song"])).bars[34:35]
        for v in b.voices2
        for e in v
        if e.heads and e.length == Fraction(1, 4)
    ]
    assert len(set(groups)) == 4
    assert len(groups) == 16


@pytest.mark.parametrize("name", names())
def test_reading_is_pinned(name: str) -> None:
    """Digest of the reading proven against the sources; contains no sheet content."""
    digest = hashlib.sha256(reading(decode(source(name))).encode()).hexdigest()[:16]
    assert digest == pins()["songs"][name]["digest"]


@pytest.mark.parametrize("name", names())
def test_derivation_matches_the_confirmed_song_list(name: str) -> None:
    pin = pins()["songs"][name]
    song = song_of(source(name))
    assert (song.tone, song.fifths, song.direction, song.semis, song.title) == (
        pin["tone"],
        pin["fifths"],
        pin["direction"],
        pin["semis"],
        pin["title"],
    )


def test_no_private_credit_reaches_the_decoded_text() -> None:
    names = (pathlib.Path(os.environ["MUSE_PINS"]).parent / "banned.txt").read_text(encoding="utf-8").split("\n")
    for name in sorted(pins()["songs"]):
        score = decode(source(name))
        words = " ".join(run[0] for bar in score.bars for text in [*bar.texts, *bar.texts2] for run in text.runs)
        assert not [word for word in names if word.strip() and word in words], name


def lead_sheets(folder: pathlib.Path) -> list[pathlib.Path]:
    """List a folder's lead sheets: a full score takes its own reader and its own tests below.

    Returns:
        The folder's PDFs that are not full scores, sorted.
    """
    return [path for path in sorted(folder.glob("*.pdf")) if not song.is_orchestral(path)]


def test_no_cue_joins_a_word_across_two_source_spans() -> None:
    root = os.environ["MUSE_SOURCES"]
    joined = []
    for folder in root.split(":"):
        for path in lead_sheets(pathlib.Path(folder)):
            for bar in decode(path).bars:
                for text in [*bar.texts, *bar.texts2]:
                    joined += [
                        (path.stem, bar.number)
                        for left, right in zip(text.runs, text.runs[1:], strict=False)
                        if left[0] and right[0] and left[0][-1].isalnum() and right[0][0].isalnum()
                    ]
    assert not joined


def test_every_source_reads_with_no_flag() -> None:
    flagged = {
        path.stem: [issue.message for issue in decode(path).flags]
        for folder in os.environ["MUSE_SOURCES"].split(os.pathsep)
        for path in lead_sheets(pathlib.Path(folder))
    }
    assert not {name: flags for name, flags in flagged.items() if flags}


def test_every_change_of_bar_length_prints_its_time_signature() -> None:
    silent = []
    for folder in os.environ["MUSE_SOURCES"].split(os.pathsep):
        for path in lead_sheets(pathlib.Path(folder)):
            bars = decode(path).bars
            silent += [
                (path.stem, after.number)
                for before, after in itertools.pairwise(bars)
                if before.length != after.length and after.time is None
            ]
    assert not silent


def printed_line_starts(path: pathlib.Path) -> list[int]:
    """Read the bar numbers a source prints at the left of each line, in reading order.

    Returns:
        The printed bar numbers, sorted by page and height.
    """
    found = []
    for at, page in enumerate(pymupdf.open(path)):
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                found += [
                    (at, span["origin"][1], int(span["text"]))
                    for span in line["spans"]
                    if span["font"] == "Edwin-Italic" and span["text"].strip().isdigit() and span["origin"][0] < 60
                ]
    return [number for _, _, number in sorted(found)]


def test_every_leland_source_starts_its_lines_on_the_printed_bar_numbers() -> None:
    sources = [
        path
        for folder in os.environ["MUSE_SOURCES"].split(os.pathsep)
        for path in sorted(pathlib.Path(folder).glob("*.pdf"))
        if encoding(path) == "leland"
    ]
    assert sources, "no Leland source found: the guard would judge nothing"
    wrong = {}
    for path in sources:
        printed = printed_line_starts(path)
        decoded = [bar.number for bar in decode(path).bars if bar.new_system]
        if not printed or decoded != printed:
            wrong[path.stem] = (printed, decoded)
    assert not wrong


def test_every_leland_source_decodes_each_printed_ending_once() -> None:
    sources = [
        path
        for folder in os.environ["MUSE_SOURCES"].split(os.pathsep)
        for path in sorted(pathlib.Path(folder).glob("*.pdf"))
        if encoding(path) == "leland"
    ]
    assert sources, "no Leland source found: the guard would judge nothing"
    wrong = {}
    for path in sources:
        printed = sum(
            1
            for page in pymupdf.open(path)
            for block in page.get_text("dict")["blocks"]
            for line in block.get("lines", [])
            for span in line["spans"]
            if span["text"].strip() in {"1.", "2."} and "Bold" in span["font"]
        )
        decoded = sum(1 for bar in decode(path).bars if bar.volta)
        if printed != decoded:
            wrong[path.stem] = (printed, decoded)
    assert not wrong


def test_every_leland_source_decodes_each_printed_repeat_barline_once() -> None:
    sources = [
        path
        for folder in os.environ["MUSE_SOURCES"].split(os.pathsep)
        for path in sorted(pathlib.Path(folder).glob("*.pdf"))
        if encoding(path) == "leland"
    ]
    assert sources, "no Leland source found: the guard would judge nothing"
    wrong = {}
    for path in sources:
        printed = 0
        for page in pymupdf.open(path):
            dots = sorted(
                (c["origin"][0], c["origin"][1])
                for block in page.get_text("rawdict")["blocks"]
                for line in block.get("lines", [])
                for span in line["spans"]
                for c in span["chars"]
                if ord(c["c"]) == leland.REPEAT_DOT
            )
            barlines: list[tuple[float, float]] = []
            for x, y in dots:
                if not any(abs(x - bx) < 2 and abs(y - by) < 120 for bx, by in barlines):
                    barlines.append((x, y))
            printed += len(barlines)
        bars = decode(path).bars
        decoded = sum(1 for bar in bars if bar.repeat_start) + sum(1 for bar in bars if bar.repeat_end)
        if printed != decoded:
            wrong[path.stem] = (printed, decoded)
    assert not wrong


def test_every_leland_repeat_end_after_another_has_its_own_start() -> None:
    sources = [
        path
        for folder in os.environ["MUSE_SOURCES"].split(os.pathsep)
        for path in sorted(pathlib.Path(folder).glob("*.pdf"))
        if encoding(path) == "leland"
    ]
    assert sources, "no Leland source found: the guard would judge nothing"
    wrong = {}
    for path in sources:
        open_start, closed = False, False
        for bar in decode(path).bars:
            if bar.repeat_start:
                open_start = True
            if bar.repeat_end:
                if closed and not open_start:
                    wrong.setdefault(path.stem, []).append(bar.number)
                open_start, closed = False, True
    assert not wrong


SYMBOL_LYRIC_FONTS = ("Times New Roman Italic+FPEF", "CIDFont+F8")
SYMBOL_LYRIC_SIZE = 10.8


def lyric_spans(path: pathlib.Path) -> list[str]:
    """Collect every lyric hint a source prints, known by the font and size it is set in.

    FreeSerif in Leland sources, italic Times at 10.8pt in symbol-font ones.

    Returns:
        The stripped text of every lyric span longer than 3 characters.
    """

    def lyric(span: dict) -> bool:
        return is_lyric(span["font"]) or (
            span["font"] in SYMBOL_LYRIC_FONTS
            and abs(span["size"] - SYMBOL_LYRIC_SIZE) < 0.3
            and " " in span["text"].strip()
        )

    return [
        span["text"].strip()
        for page in pymupdf.open(path)
        for block in page.get_text("dict")["blocks"]
        for line in block.get("lines", [])
        for span in line["spans"]
        if lyric(span) and len(span["text"].strip()) > 3
    ]


def test_every_lyric_hint_reads_under_the_staff_it_is_printed_under() -> None:
    """A lyric filed under the next line, or over the next staff, puts a performer in early."""
    judged, lost, above = 0, [], []
    for folder in os.environ["MUSE_SOURCES"].split(os.pathsep):
        for path in lead_sheets(pathlib.Path(folder)):
            texts = [(bar.number, text) for bar in decode(path).bars for text in [*bar.texts, *bar.texts2]]
            for lyric in lyric_spans(path):
                judged += 1
                found = [(number, text) for number, text in texts if lyric[:12] in "".join(run[0] for run in text.runs)]
                lost += [] if found else [(path.stem, lyric)]
                above += [(path.stem, number, lyric) for number, text in found if text.above]
    assert judged, "no lyric hint found in any source: the guard would judge nothing"
    assert not lost
    assert not above


def boxed_labels(path: pathlib.Path) -> list[str]:
    """Collect every bold label a source prints alone inside a drawn box: its section marks.

    A parenthesised instruction in a box is a cue.

    Returns:
        The text of each boxed all-bold label.
    """
    found = []
    for page in pymupdf.open(path):
        boxes = [d["rect"] for d in page.get_drawings() if 8 < d["rect"].width < 140 and 8 < d["rect"].height < 30]
        held: dict[int, list[tuple[str, bool]]] = {}
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    x, y = span["origin"]
                    at = next((n for n, r in enumerate(boxes) if r.x0 - 1 <= x <= r.x1 and r.y0 <= y <= r.y1 + 1), None)
                    if at is not None and span["text"].strip():
                        held.setdefault(at, []).append((span["text"].strip(), "Bold" in span["font"]))
        for pieces in held.values():
            text = " ".join(piece for piece, _ in pieces)
            if (
                all(bold for _, bold in pieces)
                and len(text) > 1
                and not text.rstrip(".").isdigit()
                and not text.startswith("(")
            ):
                found.append(text)
    return found


def test_every_boxed_section_label_reads_as_a_section_mark() -> None:
    """Hold every boxed label to a section mark.

    A boxed label read as a plain cue neither starts its line nor prints as a section,
    and a performer misses the interlude.
    """
    judged, lost = 0, []
    for folder in os.environ["MUSE_SOURCES"].split(os.pathsep):
        for path in lead_sheets(pathlib.Path(folder)):
            marks = [mark for bar in decode(path).bars for mark in bar.rehearsal]
            for label in boxed_labels(path):
                judged += 1
                lost += [] if label in marks else [(path.stem, label)]
    assert judged, "no boxed label found in any source: the guard would judge nothing"
    assert not lost


def orchestral_sources() -> list[str]:
    """List every full score among the private sources: each PDF of an orchestral folder.

    Returns:
        The sorted paths, or 1 placeholder naming why there are none.
    """
    root = os.environ.get("MUSE_SOURCES")
    if not root:
        return ["MUSE_SOURCES is not set"]
    return sorted(
        str(pdf)
        for folder in root.split(os.pathsep)
        for pdf in pathlib.Path(folder).glob("*.pdf")
        if song.is_orchestral(pdf)
    ) or ["no orchestral source"]


def orchestral_pin(path: str) -> dict[str, Any]:
    """Load the private pin of 1 full score.

    The pin holds its parts, its bar count, its crossing bars and the spike reader it is held equal to.

    Returns:
        The score's orchestral pin entry.
    """
    if not os.environ.get("MUSE_PINS"):
        pytest.fail("MUSE_PINS is not set")
    found = pins().get("orchestral", {}).get(pathlib.Path(path).stem)
    if found is None:
        pytest.fail(f"MUSE_PINS has no orchestral entry for {pathlib.Path(path).name}")
    return found


@pytest.mark.parametrize("path", orchestral_sources())
def test_full_score_reads_every_part_through_every_bar(path: str) -> None:
    pin = orchestral_pin(path)
    score = song.orchestra_of(pathlib.Path(path))
    assert [(p.name, p.staves, p.key) for p in score.parts] == [tuple(row) for row in pin["parts"]]
    for part in score.parts:
        assert len(part.bars) == pin["bars"]
        for b in part.bars:
            for voices in (b.voices, b.voices2):
                for v in voices:
                    assert sum((e.length for e in v), Fraction(0)) == b.length, (part.name, b.number)


def spike_reader(pin: dict[str, Any]) -> dict[str, Any]:
    """Load the proven spike reader's functions from the private repo without running its report.

    Returns:
        The namespace the reader's code defines.
    """
    text = (pathlib.Path(os.environ["MUSE_PINS"]).parent / pin["reader"]).read_text(encoding="utf-8")
    space: dict[str, Any] = {}
    exec(compile(text.rsplit("\nmain()", 1)[0], pin["reader"], "exec"), space)  # ruff: ignore[exec-builtin] - the private spike reader is the reference this decode is held to
    return space


def nearest_pair(staves: list[tuple[float, float]], y: float) -> tuple[float, float]:
    return min(staves, key=lambda pair: abs((pair[0] + pair[1]) / 2 - y))


def _spike_close(p: tuple[float, float], q: tuple[float, float]) -> bool:
    return abs(p[0] - q[0]) < 3 or abs(p[1] - q[1]) < 1.5


def _spike_edges(pair: tuple[float, float], sheet: tuple[Any, ...], reader: dict[str, Any]) -> list[float]:
    top, base = pair
    lines, verticals = sheet[4], sheet[5]
    rules = [v for v in verticals if v[3] >= reader["STEM_WIDTH"]]
    spans = [round(x, 0) for x, y0, y1, _ in rules if y0 - 1 <= top and y1 + 1 >= base]
    bars = sorted({
        x
        for x in spans
        if sum(1 for o, a0, a1, _ in rules if abs(o - x) < 1.5 and a1 - a0 > base - top - 1) >= 3
        or any(abs(o - x) < 1.5 and a1 - a0 > 3 * (base - top) for o, a0, a1, _ in rules)
    })
    merged = []
    for x in bars:
        if not merged or x - merged[-1] > 5:
            merged.append(x)
    right = max(b for y, _, b in lines if abs(y - top) < 0.3)
    if merged and merged[-1] < right - 5:
        merged.append(right)
    return merged


def _spike_adds_up(here: list[Any], rest: Fraction) -> bool:
    ups, downs, stemless = ([(e[1], e[4]) for e in here if e[3] == k] for k in "udn")
    split = any(_spike_close(u, d) for u in ups for d in downs) or any(
        _spike_close(n, m) for n in stemless for m in ups + downs
    )
    voice = collections.defaultdict(Fraction)
    for e in here:
        voice[e[3] if split or e[3] == "n" else "s"] += e[2]
    if not split and "n" in voice and "s" in voice:
        voice["s"] += voice.pop("n")
    totals = list(voice.values())
    return bool((totals and all(t == 4 or t + rest == 4 for t in totals)) or (not here and rest == 4))


def _spike_bars(
    found: dict[tuple[int, float, int], Fraction],
    key: tuple[int, float],
    merged: list[float],
    held: tuple[list[Any], list[Any], list[Any], tuple[float, float]],
    reader: dict[str, Any],
) -> None:
    ours, rests, brackets, pair = held
    for at, (a, b) in enumerate(itertools.pairwise(merged)):
        here = [e for e in ours if a <= e[1] < b - 0.3]
        quiet = [g for g in rests if a <= g[1] < b - 0.3]
        if not here and not quiet:
            continue
        rest = sum(
            (reader["RESTS"][g[0]] or Fraction(4))
            * next((r for s, x0, x1, r in brackets if s == pair and x0 <= g[1] <= x1), 1)
            for g in quiet
        )
        if _spike_adds_up(here, rest):
            found[(*key, at)] = sum((e[2] for e in here), Fraction(0))


def _spike_staff(
    found: dict[tuple[int, float, int], Fraction],
    number: int,
    staff: tuple[float, float],
    seen: tuple[tuple[Any, ...], list[tuple[float, float]], tuple[list[Any], list[Any]]],
    reader: dict[str, Any],
) -> None:
    sheet, staves, (sounding, brackets) = seen
    top, base = staff
    pair = (top, base)
    rests = [g for g in sheet[0] if g[0] in reader["RESTS"] and nearest_pair(staves, g[2]) == pair]
    merged = _spike_edges(pair, sheet, reader)
    ours = [e for e in sounding if e[0] == pair]
    _spike_bars(found, (number, round(top, 1)), merged, (ours, rests, brackets, pair), reader)


def spike_staff_bars(path: str, reader: dict[str, Any]) -> dict[tuple[int, float, int], Fraction]:
    """Find every staff-bar the spike reader adds up on, by page, staff and bar, with the sounding quarters it reads there.

    Returns:
        The sounding quarters keyed by page number, staff top and bar index.
    """
    found: dict[tuple[int, float, int], Fraction] = {}
    for page in pymupdf.open(path):
        sheet = reader["read"](page)
        staves = reader["staves"](sheet[4])
        if not staves:
            continue
        events = reader["events"](sheet[0], sheet[2], sheet[3], sheet[5], sheet[6], sheet[7], staves)
        for staff in staves:
            _spike_staff(found, page.number, staff, (sheet, staves, events), reader)
    return found


@pytest.mark.parametrize("path", orchestral_sources())
def test_full_score_adds_up_on_every_staff_bar_the_spike_reader_adds_up_on(path: str) -> None:
    pin = orchestral_pin(path)
    spike = spike_staff_bars(path, spike_reader(pin))
    assert len(spike) == pin["spike_whole"]
    doc = pymupdf.open(path)
    role = orchestral.roles(doc)
    ours = {}
    for page in doc:
        sheet = orchestral.read(page, role)
        staves = orchestral.staves(sheet.lines)
        if not staves:
            continue
        braces = orchestral.brace_spans(page, role)
        sounds, spans = orchestral.sounds(sheet, staves, braces)
        rests = orchestral.rests_of(sheet, staves, spans)
        for system in orchestral.systems_of(page.number, sheet, staves, braces):
            edges = orchestral.system_bars(system, sheet.verticals)
            for staff in system.staves:
                for at, (a, b) in enumerate(itertools.pairwise(edges)):
                    here = [s for s in sounds if s.staff == staff and a <= s.x < b - 0.3]
                    quiet = [r for r in rests if r.staff == staff and a <= r.x < b - 0.3]
                    try:
                        orchestral.voices_of(here, quiet, Fraction(4), "parity")
                    except orchestral.Unreadable:
                        continue
                    ours[page.number, round(staff.top, 1), at] = sum(
                        (s.nominal * (s.ratio or 1) for s in here), Fraction(0)
                    )
    assert {key: ours.get(key) for key in spike} == spike


@pytest.mark.parametrize("path", orchestral_sources())
def test_a_line_crossing_a_braced_pair_reads_as_1_line(path: str) -> None:
    pin = orchestral_pin(path)
    score = song.orchestra_of(pathlib.Path(path))
    for name, numbers in pin["crossing"].items():
        part = next(p for p in score.parts if p.name == name)
        crossing = [b.number for b in part.bars if b.voices2 == []]
        assert crossing == numbers
        for number in numbers:
            (line,) = part.bars[number - 1].voices
            assert {e.staff for e in line} == {1, 2}
            assert all(e.heads for e in line)
            assert sum((e.length for e in line), Fraction(0)) == part.bars[number - 1].length


@pytest.mark.musescore
@pytest.mark.parametrize("path", orchestral_sources())
def test_full_score_builds_3_files_that_hold_the_decode_and_the_source_s_systems(
    path: str, tmp_path: pathlib.Path
) -> None:
    src = pathlib.Path(path)
    info = song.song_of(src)
    out, work = tmp_path / "out", tmp_path / "work"
    (out / info.name).mkdir(parents=True)
    work.mkdir()
    fullscore.build(src, out, work, info)
    assert all((out / name).stat().st_size for name in info.files())
    score = song.orchestra_of(src)
    exported = work / "final.musicxml"
    launch.mscore(["-o", str(exported), str(out / info.score_file)], launch.LAUNCH_SECONDS, check=True)
    assert fullcheck.content_problems(score, exported) == []
    probe = work / "final.mpos"
    launch.mscore(["-o", str(probe), str(out / info.score_file)], launch.LAUNCH_SECONDS, check=True)
    assert fullscore.system_problems(score, probe) == []


@pytest.mark.parametrize("path", orchestral_sources())
def test_every_orchestral_note_lies_in_a_playable_octave(path: str) -> None:
    """A clef of another system read on this staff throws every note dozens of octaves off."""
    score = song.orchestra_of(pathlib.Path(path))
    heads = [
        (part.name, bar.number, head.octave)
        for part in score.parts
        for bar in part.bars
        for voices in (bar.voices, bar.voices2)
        for voice in voices
        for event in voice
        for head in event.heads
    ]
    assert heads, "no note read: the guard would judge nothing"
    assert not [head for head in heads if not 0 <= head[2] <= 8]


@pytest.mark.parametrize("path", orchestral_sources())
def test_every_printed_mark_reaches_its_note(path: str) -> None:
    """A bowing or articulation lost, or given to another part, changes how the part is played."""
    glyphs = {
        symbolfont.TENUTO: "tenuto",
        symbolfont.MARCATO: "strong-accent-up",
        symbolfont.MARCATO_BELOW: "strong-accent-down",
        symbolfont.UP_BOW: "up-bow",
        symbolfont.DOWN_BOW: "down-bow",
        symbolfont.HARMONIC: "harmonic",
    }
    printed: collections.Counter[str] = collections.Counter()
    waves = set()
    chars = (
        (number, char)
        for number, page in enumerate(pymupdf.open(path))
        for block in page.get_text("rawdict")["blocks"]
        for line in block.get("lines", [])
        for span in line["spans"]
        for char in span["chars"]
    )
    for number, char in chars:
        code = ord(char["c"])
        printed[glyphs.get(code, "")] += code in glyphs
        if code == symbolfont.ARPEGGIO:
            waves.add((number, round(char["origin"][0])))
    score = song.orchestra_of(pathlib.Path(path))
    read = collections.Counter(
        mark
        for part in score.parts
        for bar in part.bars
        for voices in (bar.voices, bar.voices2)
        for voice in voices
        for event in voice
        for mark in event.marks
    )
    assert sum(printed.values()), "no mark printed: the guard would judge nothing"
    assert {mark: read[mark] for mark in glyphs.values()} == {mark: printed[mark] for mark in glyphs.values()}
    assert read["arpeggiate"] >= len(waves)


@pytest.mark.parametrize("path", orchestral_sources())
def test_every_hairpin_reads_as_a_wedge_with_its_end(path: str) -> None:
    """A hairpin dropped from a part loses its swell; counted here from the raw strokes, apart from the reader."""
    pins = {"crescendo": 0, "diminuendo": 0}
    for page in pymupdf.open(path):
        legs = []
        for drawing in page.get_drawings():
            items = drawing["items"]
            if len(items) == 1 and items[0][0] == "l":
                a, b = sorted((items[0][1], items[0][2]), key=lambda point: point.x)
                if 0.5 < abs(a.y - b.y) < 9 and b.x - a.x > 10:
                    legs.append((a, b))
        for at, (a, b) in enumerate(legs):
            for c, d in legs[at + 1 :]:
                if abs(a.x - c.x) < 1 and abs(a.y - c.y) < 1 and abs(b.y - d.y) > 1.5:
                    pins["crescendo"] += 1
                elif abs(b.x - d.x) < 1 and abs(b.y - d.y) < 1 and abs(a.y - c.y) > 1.5:
                    pins["diminuendo"] += 1
    score = song.orchestra_of(pathlib.Path(path))
    kinds = [text.kind for part in score.parts for bar in part.bars for text in [*bar.texts, *bar.texts2]]
    assert sum(pins.values()), "no hairpin printed: the guard would judge nothing"
    assert {kind: kinds.count(kind) for kind in pins} == pins
    assert kinds.count("wedge-stop") == sum(pins.values())
