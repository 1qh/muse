"""Decoders against the real sheets, kept outside the repo; MUSE_SOURCES must name them."""
import collections
import hashlib
import json
import os
import pathlib
from fractions import Fraction as F

import pymupdf
import pytest

from muse import orchestral, song
from muse.song import decode, song_of


def pins():
    path = os.environ.get("MUSE_PINS")
    if not path:
        pytest.fail("MUSE_PINS is not set")
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))


def names():
    path = os.environ.get("MUSE_PINS")
    return sorted(json.loads(pathlib.Path(path).read_text(encoding="utf-8"))["songs"]) if path else ["MUSE_PINS is not set"]


def source(name):
    root = os.environ.get("MUSE_SOURCES")
    if not root:
        pytest.fail("MUSE_SOURCES is not set")
    for folder in root.split(os.pathsep):
        path = pathlib.Path(folder) / f"{name}.pdf"
        if path.exists():
            return path
    pytest.fail(f"{name} is in no source folder")


def reading(score):
    """Stable text form of everything a decoder reads."""
    rows = []
    for b in score.bars:
        for voices in (b.voices, b.voices2):
            for v in voices:
                rows.append(" ".join(("+".join(f"{h.step}{h.alter}{h.octave}" for h in e.heads) or "r") + f":{e.length}" for e in v))
        rows.append("|".join(f"{c.root}{c.root_alter}{c.kind}/{c.bass}{c.bass_alter}" for c in b.chords))
        rows.append(f"{b.repeat_start}{b.repeat_end}{b.volta}{b.segno}{b.coda}{b.to_coda}{b.dal_segno}")
        rows.append("|".join(f"{'^' if text.above else 'v'}" + "".join(run[0] for run in text.runs) for text in [*b.texts, *b.texts2]))
        rows.append("|".join(b.rehearsal))
    return "\n".join(rows)


@pytest.mark.parametrize("name", names())
def test_bar_count_and_no_flags(name):
    score = decode(source(name))
    assert len(score.bars) == pins()["songs"][name]["bars"]
    assert score.flags == []


@pytest.mark.parametrize("name", names())
def test_every_voice_fills_its_bar(name):
    for b in decode(source(name)).bars:
        for voices in (b.voices, b.voices2):
            for v in voices:
                assert sum((e.length for e in v), F(0)) == b.length, b.number


def test_all_notes_of_a_beam_share_a_group():
    groups = [e.beam for b in decode(source(pins()["beam_song"])).bars[34:35] for v in b.voices2 for e in v if e.heads and e.length == F(1, 4)]
    assert len(set(groups)) == 4 and len(groups) == 16


@pytest.mark.parametrize("name", names())
def test_reading_is_pinned(name):
    """Digest of the reading proven against the sources; contains no sheet content."""
    digest = hashlib.sha256(reading(decode(source(name))).encode()).hexdigest()[:16]
    assert digest == pins()["songs"][name]["digest"]


@pytest.mark.parametrize("name", names())
def test_derivation_matches_the_confirmed_song_list(name):
    pin = pins()["songs"][name]
    song = song_of(source(name))
    assert (song.tone, song.fifths, song.direction, song.semis, song.title) == (pin["tone"], pin["fifths"], pin["direction"], pin["semis"], pin["title"])


def test_no_private_credit_reaches_the_decoded_text() -> None:
    names = (pathlib.Path(os.environ["MUSE_PINS"]).parent / "banned.txt").read_text(encoding="utf-8").split("\n")
    for name in sorted(pins()["songs"]):
        score = decode(source(name))
        words = " ".join(run[0] for bar in score.bars for text in [*bar.texts, *bar.texts2] for run in text.runs)
        assert not [word for word in names if word.strip() and word in words], name


def lead_sheets(folder: pathlib.Path) -> list[pathlib.Path]:
    """A folder's lead sheets: a full score takes its own reader and its own tests below."""
    return [path for path in sorted(folder.glob("*.pdf")) if not song.is_orchestral(path)]


def test_no_cue_joins_a_word_across_two_source_spans() -> None:
    root = os.environ["MUSE_SOURCES"]
    joined = []
    for folder in root.split(":"):
        for path in lead_sheets(pathlib.Path(folder)):
            for bar in decode(path).bars:
                for text in [*bar.texts, *bar.texts2]:
                    joined += [(path.stem, bar.number) for left, right in zip(text.runs, text.runs[1:], strict=False) if left[0] and right[0] and left[0][-1].isalnum() and right[0][0].isalnum()]
    assert not joined


def test_every_source_reads_with_no_flag() -> None:
    flagged = {path.stem: [issue.message for issue in decode(path).flags] for folder in os.environ["MUSE_SOURCES"].split(os.pathsep) for path in lead_sheets(pathlib.Path(folder))}
    assert not {name: flags for name, flags in flagged.items() if flags}


def test_every_change_of_bar_length_prints_its_time_signature() -> None:
    silent = []
    for folder in os.environ["MUSE_SOURCES"].split(os.pathsep):
        for path in lead_sheets(pathlib.Path(folder)):
            bars = decode(path).bars
            silent += [(path.stem, after.number) for before, after in zip(bars, bars[1:], strict=False) if before.length != after.length and after.time is None]
    assert not silent


def printed_line_starts(path: pathlib.Path) -> list[int]:
    """The bar numbers a source prints at the left of each line, in reading order."""
    import pymupdf

    found = []
    for at, page in enumerate(pymupdf.open(path)):
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                found += [(at, span["origin"][1], int(span["text"])) for span in line["spans"] if span["font"] == "Edwin-Italic" and span["text"].strip().isdigit() and span["origin"][0] < 60]
    return [number for _, _, number in sorted(found)]


def test_every_leland_source_starts_its_lines_on_the_printed_bar_numbers() -> None:
    from muse.song import encoding

    sources = [path for folder in os.environ["MUSE_SOURCES"].split(os.pathsep) for path in sorted(pathlib.Path(folder).glob("*.pdf")) if encoding(path) == "leland"]
    assert sources, "no Leland source found: the guard would judge nothing"
    wrong = {}
    for path in sources:
        printed = printed_line_starts(path)
        decoded = [bar.number for bar in decode(path).bars if bar.new_system]
        if not printed or decoded != printed:
            wrong[path.stem] = (printed, decoded)
    assert not wrong


def test_every_leland_source_decodes_each_printed_ending_once() -> None:
    import pymupdf

    from muse.song import encoding

    sources = [path for folder in os.environ["MUSE_SOURCES"].split(os.pathsep) for path in sorted(pathlib.Path(folder).glob("*.pdf")) if encoding(path) == "leland"]
    assert sources, "no Leland source found: the guard would judge nothing"
    wrong = {}
    for path in sources:
        printed = sum(1 for page in pymupdf.open(path) for block in page.get_text("dict")["blocks"] for line in block.get("lines", []) for span in line["spans"] if span["text"].strip() in ("1.", "2.") and "Bold" in span["font"])
        decoded = sum(1 for bar in decode(path).bars if bar.volta)
        if printed != decoded:
            wrong[path.stem] = (printed, decoded)
    assert not wrong


def test_every_leland_source_decodes_each_printed_repeat_barline_once() -> None:
    import pymupdf

    from muse import leland
    from muse.song import encoding

    sources = [path for folder in os.environ["MUSE_SOURCES"].split(os.pathsep) for path in sorted(pathlib.Path(folder).glob("*.pdf")) if encoding(path) == "leland"]
    assert sources, "no Leland source found: the guard would judge nothing"
    wrong = {}
    for path in sources:
        printed = 0
        for page in pymupdf.open(path):
            dots = sorted((c["origin"][0], c["origin"][1]) for block in page.get_text("rawdict")["blocks"] for line in block.get("lines", []) for span in line["spans"] for c in span["chars"] if ord(c["c"]) == leland.REPEAT_DOT)
            barlines: list[tuple[float, float]] = []  # 1 per printed repeat barline: its dots on every staff of the line stand in 1 column
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
    from muse.song import encoding

    sources = [path for folder in os.environ["MUSE_SOURCES"].split(os.pathsep) for path in sorted(pathlib.Path(folder).glob("*.pdf")) if encoding(path) == "leland"]
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


SYMBOL_LYRIC_FONTS = ("Times New Roman Italic+FPEF", "CIDFont+F8")  # the italic hint line of the symbol-font sources, at 10.8pt
SYMBOL_LYRIC_SIZE = 10.8


def lyric_spans(path: pathlib.Path) -> list[str]:
    """Every lyric hint a source prints, known by the font and size it is set in: FreeSerif in Leland sources, italic Times at 10.8pt in symbol-font ones."""
    import pymupdf

    from muse.leland import is_lyric

    def lyric(span: dict) -> bool:
        return is_lyric(span["font"]) or (span["font"] in SYMBOL_LYRIC_FONTS and abs(span["size"] - SYMBOL_LYRIC_SIZE) < 0.3 and " " in span["text"].strip())

    return [span["text"].strip() for page in pymupdf.open(path) for block in page.get_text("dict")["blocks"] for line in block.get("lines", []) for span in line["spans"] if lyric(span) and len(span["text"].strip()) > 3]


def test_every_lyric_hint_reads_under_the_staff_it_is_printed_under() -> None:
    """A lyric filed under the next line, or over the next staff, puts a performer in early."""
    judged, lost, above = 0, [], []
    for folder in os.environ["MUSE_SOURCES"].split(os.pathsep):
        for path in sorted(pathlib.Path(folder).glob("*.pdf")):
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
    """Every bold label a source prints alone inside a drawn box: its section marks; a parenthesised instruction in a box is a cue."""
    import pymupdf

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
            if all(bold for _, bold in pieces) and len(text) > 1 and not text.rstrip(".").isdigit() and not text.startswith("("):
                found.append(text)
    return found


def test_every_boxed_section_label_reads_as_a_section_mark() -> None:
    """A boxed label read as a plain cue neither starts its line nor prints as a section, and a performer misses the interlude."""
    judged, lost = 0, []
    for folder in os.environ["MUSE_SOURCES"].split(os.pathsep):
        for path in sorted(pathlib.Path(folder).glob("*.pdf")):
            marks = [mark for bar in decode(path).bars for mark in bar.rehearsal]
            for label in boxed_labels(path):
                judged += 1
                lost += [] if label in marks else [(path.stem, label)]
    assert judged, "no boxed label found in any source: the guard would judge nothing"
    assert not lost


def orchestral_sources():
    """Every full score among the private sources: each PDF of an orchestral folder."""
    root = os.environ.get("MUSE_SOURCES")
    if not root:
        return ["MUSE_SOURCES is not set"]
    return sorted(str(pdf) for folder in root.split(os.pathsep) for pdf in pathlib.Path(folder).glob("*.pdf") if song.is_orchestral(pdf)) or ["no orchestral source"]


def orchestral_pin(path):
    """The private pin of 1 full score: its parts, its bar count, its crossing bars and the spike reader it is held equal to."""
    if not os.environ.get("MUSE_PINS"):
        pytest.fail("MUSE_PINS is not set")
    found = pins().get("orchestral", {}).get(pathlib.Path(path).stem)
    if found is None:
        pytest.fail(f"MUSE_PINS has no orchestral entry for {pathlib.Path(path).name}")
    return found


@pytest.mark.parametrize("path", orchestral_sources())
def test_full_score_reads_every_part_through_every_bar(path):
    pin = orchestral_pin(path)
    score = song.orchestra_of(pathlib.Path(path))
    assert [(p.name, p.staves, p.key) for p in score.parts] == [tuple(row) for row in pin["parts"]]
    for part in score.parts:
        assert len(part.bars) == pin["bars"]
        for b in part.bars:
            for voices in (b.voices, b.voices2):
                for v in voices:
                    assert sum((e.length for e in v), F(0)) == b.length, (part.name, b.number)


def spike_reader(pin):
    """The proven spike reader's functions, loaded from the private repo without running its report."""
    text = (pathlib.Path(os.environ["MUSE_PINS"]).parent / pin["reader"]).read_text(encoding="utf-8")
    space = {}
    exec(compile(text.rsplit("\nmain()", 1)[0], pin["reader"], "exec"), space)  # noqa: S102 - the private spike reader is the reference this decode is held to
    return space


def nearest_pair(staves, y):
    return min(staves, key=lambda pair: abs((pair[0] + pair[1]) / 2 - y))


def spike_staff_bars(path, reader):
    """Every staff-bar the spike reader adds up on, by page, staff and bar, with the sounding quarters it reads there."""
    found = {}
    for page in pymupdf.open(path):
        glyphs, _, digits, braces, lines, verticals, beams, ticks = reader["read"](page)
        staves = reader["staves"](lines)
        if not staves:
            continue
        sounding, brackets = reader["events"](glyphs, digits, braces, verticals, beams, ticks, staves)
        for top, base in staves:
            rests = [g for g in glyphs if g[0] in reader["RESTS"] and nearest_pair(staves, g[2]) == (top, base)]
            rules = [v for v in verticals if v[3] >= reader["STEM_WIDTH"]]
            spans = [round(x, 0) for x, y0, y1, _ in rules if y0 - 1 <= top and y1 + 1 >= base]
            bars = sorted({x for x in spans if sum(1 for o, a0, a1, _ in rules if abs(o - x) < 1.5 and a1 - a0 > base - top - 1) >= 3 or any(abs(o - x) < 1.5 and a1 - a0 > 3 * (base - top) for o, a0, a1, _ in rules)})
            merged = []
            for x in bars:
                if not merged or x - merged[-1] > 5:
                    merged.append(x)
            right = max(b for y, _, b in lines if abs(y - top) < 0.3)
            if merged and merged[-1] < right - 5:
                merged.append(right)
            ours = [e for e in sounding if e[0] == (top, base)]
            for at, (a, b) in enumerate(zip(merged, merged[1:], strict=False)):
                here = [e for e in ours if a <= e[1] < b - 0.3]
                quiet = [g for g in rests if a <= g[1] < b - 0.3]
                if not here and not quiet:
                    continue
                rest = sum((reader["RESTS"][g[0]] or F(4)) * next((r for s, x0, x1, r in brackets if s == (top, base) and x0 <= g[1] <= x1), 1) for g in quiet)
                close = lambda p, q: abs(p[0] - q[0]) < 3 or abs(p[1] - q[1]) < 1.5  # noqa: E731
                ups, downs, stemless = ([(e[1], e[4]) for e in here if e[3] == k] for k in "udn")
                split = any(close(u, d) for u in ups for d in downs) or any(close(n, m) for n in stemless for m in ups + downs)
                voice = collections.defaultdict(F)
                for e in here:
                    voice[e[3] if split or e[3] == "n" else "s"] += e[2]
                if not split and "n" in voice and "s" in voice:
                    voice["s"] += voice.pop("n")
                totals = list(voice.values())
                if (totals and all(t == 4 or t + rest == 4 for t in totals)) or (not here and rest == 4):
                    found[(page.number, round(top, 1), at)] = sum((e[2] for e in here), F(0))
    return found


@pytest.mark.parametrize("path", orchestral_sources())
def test_full_score_adds_up_on_every_staff_bar_the_spike_reader_adds_up_on(path):
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
                for at, (a, b) in enumerate(zip(edges, edges[1:], strict=False)):
                    here = [s for s in sounds if s.staff == staff and a <= s.x < b - 0.3]
                    quiet = [r for r in rests if r.staff == staff and a <= r.x < b - 0.3]
                    try:
                        orchestral.voices_of(here, quiet, F(4), "parity")
                    except orchestral.Unreadable:
                        continue
                    ours[(page.number, round(staff.top, 1), at)] = sum((s.nominal * (s.ratio or 1) for s in here), F(0))
    assert {key: ours.get(key) for key in spike} == spike


@pytest.mark.parametrize("path", orchestral_sources())
def test_a_line_crossing_a_braced_pair_reads_as_1_line(path):
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
            assert sum((e.length for e in line), F(0)) == part.bars[number - 1].length


@pytest.mark.musescore
@pytest.mark.parametrize("path", orchestral_sources())
def test_full_score_builds_3_files_that_hold_the_decode_and_the_source_s_systems(path, tmp_path):
    from muse import fullcheck, fullscore, launch, port

    src = pathlib.Path(path)
    info = song.song_of(src)
    out, work = tmp_path / "out", tmp_path / "work"
    (out / info.name).mkdir(parents=True)
    work.mkdir()
    report = fullscore.build(src, out, work, info)
    assert "legacy" not in report, report
    assert all((out / name).stat().st_size for name in info.files())
    score = song.orchestra_of(src)
    exported = work / "final.musicxml"
    launch.mscore(["-o", str(exported), str(out / info.score_file)], port.LAUNCH_SECONDS, check=True)
    assert fullcheck.content_problems(score, exported) == []
    probe = work / "final.mpos"
    launch.mscore(["-o", str(probe), str(out / info.score_file)], port.LAUNCH_SECONDS, check=True)
    assert fullcheck.system_problems(score, probe) == []
