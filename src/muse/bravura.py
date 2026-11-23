"""Decode the Bravura vector sheet (MuseScore-engraved, treble-8 clef) into the score model."""

from __future__ import annotations

import dataclasses
import itertools
import operator
import re
from fractions import Fraction
from typing import Any

import pymupdf

from muse import leland
from muse.model import LETTERS, Bar, Chord, Event, Head, Issue, Score, Text, UnknownGlyphError, bar_at, mark_bar
from muse.smufl import BAR_REST, RESTS, TIME_DIGITS
from muse.symbolfont import parse_chord

CREDIT_ROW = 14.0
BLACK, HALF = 0xF4BE, 0xF4BD
FLAG_UP = {0xE240: 1, 0xE242: 2}
FLAG_DOWN = {0xE241: 1, 0xE243: 2}
KNOWN_GLYPHS = frozenset((
    0xE003,
    0xE004,
    0xE005,
    0xE006,
    0xE044,
    0xE047,
    0xE048,
    0xE052,
    0xE082,
    0xE084,
    0xE240,
    0xE241,
    0xE242,
    0xE243,
    0xE262,
    0xE4A1,
    0xE4C0,
    0xE4E3,
    0xE4E4,
    0xE4E5,
    0xE4E6,
    0xE4E7,
    0xE873,
    0xE87B,
    0xE883,
    0xECA5,
    0xF4BD,
    0xF4BE,
))


REPEAT_START = 0xE003
REPEAT_END = 0xE005
SEGNO = 0xE047
CODA = 0xE048
CHORD_MAJ7 = 0xE873
CHORD_SLASH = 0xE87B
TRIPLET = 0xE883
STRAIGHT_TOLERANCE = 0.1
BARLINE_MIN_WIDTH = 0.9
BARLINE_MIN_HEIGHT = 18
BARLINE_MAX_HEIGHT = 22
BARLINE_FOOT_REACH = 2
REPEAT_ROW_REACH = 25
TIME_ROW_REACH = 25
BARLINE_MERGE_GAP = 5
ROW_SIZE_TOLERANCE = 9
REPEAT_BARLINE_REACH = 7
SEGNO_BARLINE_REACH = 16
CODA_LABEL_REACH = 6
CHORD_FONT = "Academico-BoldItalic"
CHORD_FONT_SIZE = 10.6
CHORD_FONT_SIZE_TOLERANCE = 0.3
MAJ7_X_REACH = 3
MAJ7_Y_REACH = 3
SLASH_X_REACH = 2
SLASH_BASS_X_REACH = 8
SLASH_BASS_Y_REACH = 2
GRACE_MAX_SIZE = 15
STEM_X_REACH = 7.5
STEM_UP_OFFSET = 3
BEAM_MIN_WIDTH = 8
BEAM_HITS_CAP = 3
FLAG_UP_REACH = 1
FLAG_DOWN_REACH = 1.5
ACCIDENTAL_X_REACH = 11
ACCIDENTAL_Y_REACH = 1.4
FERMATA_REACH = 7
ACCENT_REACH = 7
CHORD_MERGE_REACH = 1.2
TIE_START_X_REACH = 9
TIE_START_MAX_OFFSET = 10
TIE_END_X_REACH = 9
TIE_Y_REACH = 16

_Glyph = tuple[int, float, float, float]
_Span = tuple[str, float, float, float, float, str]


@dataclasses.dataclass
class _Sheet:
    chars: list[_Glyph]
    spans: list[_Span]
    credit_spans: set[_Span]
    v_bar: list[tuple[float, float, float]]
    v_thin: list[tuple[float, float, float]]
    fills: list[pymupdf.Rect]
    step: float
    size: float
    bars: list[Bar] = dataclasses.field(default_factory=list)
    flags: list[Issue] = dataclasses.field(default_factory=list)
    prev: Chord | None = None


@dataclasses.dataclass
class _Staff:
    base: float
    step: float
    stems: list[tuple[float, float, float]]
    fills: list[pymupdf.Rect]
    flags: list[Issue]


def decode(path: str) -> Score:
    sheet, bases, ties = _load_sheet(path)
    _read_systems(sheet, bases)
    bars = sheet.bars
    wire_ties(bars, ties)
    bars[-1].final = True
    _stop_voltas(bars)
    return Score(bars, [], sheet.flags, has_second_staff=False, clef_octave=-1)


def _load_sheet(path: str) -> tuple[_Sheet, list[float], list[pymupdf.Rect]]:
    page = pymupdf.open(path)[0]
    chars, spans = _read_text(page)
    title = max(spans, key=operator.itemgetter(4))
    credit_spans = {t for t in spans if t == title or (abs(t[2] - title[2]) < CREDIT_ROW and t[1] > title[3])}
    h_lines, v_thin, v_bar, fills, ties = _read_drawings(page)
    ys = sorted({y for y, _, _ in leland.staff_lines(h_lines)})
    bases = [ys[ys.index(top) + 4] for top in leland.staff_tops(ys)]
    step = (ys[4] - ys[0]) / 8
    size = next(c[3] for c in chars if c[0] == BLACK)
    return _Sheet(chars, spans, credit_spans, v_bar, v_thin, fills, step, size), bases, ties


def _read_systems(sheet: _Sheet, bases: list[float]) -> None:
    for s_i, base in enumerate(bases):
        above = bases[s_i - 1] if s_i else None
        _read_system(sheet, base, above)


def _read_text(page: pymupdf.Page) -> tuple[list[_Glyph], list[_Span]]:
    chars: list[_Glyph] = []
    spans: list[_Span] = []
    for block in page.get_text("rawdict")["blocks"]:
        for line in block.get("lines", []):
            for s in line["spans"]:
                if s["font"].startswith("Bravura"):
                    _read_glyphs(s, chars)
                else:
                    _read_span(s, spans)
    return chars, spans


def _read_glyphs(s: dict[str, Any], chars: list[_Glyph]) -> None:
    for ch in s["chars"]:
        if ord(ch["c"]) not in KNOWN_GLYPHS and not ch["c"].isspace():
            msg = f"music glyph {ord(ch['c']):#x} is not decoded"
            raise UnknownGlyphError(msg)
        chars.append((ord(ch["c"]), ch["origin"][0], ch["origin"][1], s["size"]))


def _read_span(s: dict[str, Any], spans: list[_Span]) -> None:
    text = "".join(c["c"] for c in s["chars"])
    if text.strip():
        spans.append((
            text,
            s["chars"][0]["origin"][0],
            s["origin"][1],
            s["bbox"][2],
            s["size"],
            s["font"],
        ))


def _read_drawings(
    page: pymupdf.Page,
) -> tuple[
    list[tuple[float, float, float]],
    list[tuple[float, float, float]],
    list[tuple[float, float, float]],
    list[pymupdf.Rect],
    list[pymupdf.Rect],
]:
    h_lines: list[tuple[float, float, float]] = []
    v_thin: list[tuple[float, float, float]] = []
    v_bar: list[tuple[float, float, float]] = []
    fills: list[pymupdf.Rect] = []
    ties: list[pymupdf.Rect] = []
    for d in page.get_drawings():
        kinds = "".join(i[0] for i in d["items"])
        r = d["rect"]
        if kinds == "l":
            _read_line(d, r, h_lines, v_thin, v_bar)
        elif (kinds == "llll" and d["type"] == "f") or kinds == "lllll":
            fills.append(r)
        elif kinds == "clcl":
            ties.append(r)
    return h_lines, v_thin, v_bar, fills, ties


def _read_line(
    d: dict[str, Any],
    r: pymupdf.Rect,
    h_lines: list[tuple[float, float, float]],
    v_thin: list[tuple[float, float, float]],
    v_bar: list[tuple[float, float, float]],
) -> None:
    a, b = d["items"][0][1], d["items"][0][2]
    if abs(a.y - b.y) < STRAIGHT_TOLERANCE:
        h_lines.append((a.y, min(a.x, b.x), max(a.x, b.x)))
    elif (
        abs(a.x - b.x) < STRAIGHT_TOLERANCE
        and d["width"] > BARLINE_MIN_WIDTH
        and BARLINE_MIN_HEIGHT < r.height < BARLINE_MAX_HEIGHT
    ):
        v_bar.append((a.x, r.y0, r.y1))
    elif abs(a.x - b.x) < STRAIGHT_TOLERANCE:
        v_thin.append((a.x, r.y0, r.y1))


def _read_system(sheet: _Sheet, base: float, above: float | None) -> None:
    cols, reps = _columns(sheet, base)
    mark_lo = base - 60 if above is None else max(base - 60, above + 14)
    note_lo = base - 50 if above is None else max(base - 50, above + 22)
    row_chars = [c for c in sheet.chars if note_lo < c[2] < base + 22 and abs(c[3] - sheet.size) < ROW_SIZE_TOLERANCE]
    row_new = _read_bars(sheet, base, cols, reps, row_chars)
    if not row_new:
        return
    _place_marks(sheet, row_new, base, mark_lo)
    row_spans = [t for t in sheet.spans if mark_lo < t[2] < base + 14]
    row_ch = [c for c in sheet.chars if c[0] in {CHORD_SLASH, CHORD_MAJ7} and mark_lo < c[2] < base - 10]
    items = [
        t
        for t in row_spans
        if t[5] == CHORD_FONT
        and abs(t[4] - CHORD_FONT_SIZE) < CHORD_FONT_SIZE_TOLERANCE
        and t[0].strip()
        and re.match(r"[A-G]", t[0].strip())
    ]
    _place_texts(sheet, row_new, row_spans, items)
    _place_chords(sheet, row_new, row_ch, items)


def _columns(sheet: _Sheet, base: float) -> tuple[list[float], list[_Glyph]]:
    xs = sorted({round(x, 1) for x, _y0, y1 in sheet.v_bar if abs(y1 - base) < BARLINE_FOOT_REACH})
    reps = [c for c in sheet.chars if c[0] in {REPEAT_START, REPEAT_END} and abs(c[2] - base) < REPEAT_ROW_REACH]
    for c in reps:
        xs.append(round(c[1] + (2 if c[0] == REPEAT_START else 3), 1))
    xs = sorted(set(xs))
    merged: list[float] = []
    for x in xs:
        if not merged or x - merged[-1] > BARLINE_MERGE_GAP:
            merged.append(x)
    next(
        (c[1] for c in sheet.chars if abs(c[2] - base) < TIME_ROW_REACH and c[0] in TIME_DIGITS),
        None,
    )
    left_edge = 58.0
    cols = [left_edge] + [m for m in merged if m > left_edge + 8]
    return cols, reps


def _read_bars(
    sheet: _Sheet,
    base: float,
    cols: list[float],
    reps: list[_Glyph],
    row_chars: list[_Glyph],
) -> list[Bar]:
    staff = _Staff(base, sheet.step, sheet.v_thin, sheet.fills, sheet.flags)
    length = Fraction(2)
    row_new: list[Bar] = []
    for a, b in itertools.pairwise(cols):
        content = [c for c in row_chars if a <= c[1] < b - 0.5]
        if not any(c[0] in {BLACK, HALF, BAR_REST} or c[0] in RESTS for c in content):
            continue
        bar = Bar(len(sheet.bars) + 1, 0, a, b, length)
        bar.new_system = not row_new and bool(sheet.bars)
        bar.base = base
        if not sheet.bars:
            bar.key = 1
            bar.time = (2, 4)
            bar.tempo = ("SlowRock", 56)
        bar.voices = read_bar(bar, content, staff)
        bar.repeat_start = any(c[0] == REPEAT_START and abs(c[1] + 2 - a) < REPEAT_BARLINE_REACH for c in reps)
        bar.repeat_end = any(c[0] == REPEAT_END and abs(c[1] + 3 - b) < REPEAT_BARLINE_REACH for c in reps)
        sheet.bars.append(bar)
        row_new.append(bar)
    return row_new


def _place_marks(sheet: _Sheet, row_new: list[Bar], base: float, mark_lo: float) -> None:
    for ch in sheet.chars:
        if mark_lo < ch[2] < base + 10 and ch[0] == SEGNO:
            near = min(row_new, key=lambda n: abs(n.left - ch[1]))
            if abs(near.left - ch[1]) < SEGNO_BARLINE_REACH:
                near.segno = True
            else:
                target = next((n for n in row_new if n.left <= ch[1] < n.right + 2), row_new[-1])
                target.dal_segno = True
        if mark_lo < ch[2] < base + 10 and ch[0] == CODA:
            target = bar_at(row_new, ch[1], 6)
            if any(sp[0].strip().startswith("Coda") and abs(sp[2] - ch[2]) < CODA_LABEL_REACH for sp in sheet.spans):
                target.coda = True
            else:
                target.to_coda = True


def _place_texts(sheet: _Sheet, row_new: list[Bar], row_spans: list[_Span], items: list[_Span]) -> None:
    for t in row_spans:
        if t in items or not t[0].strip():
            continue
        text = t[0].strip()
        if t in sheet.credit_spans or "=" in text or re.fullmatch(r"\d+", text) or text in {"A", "B"}:
            if text in {"A", "B"}:
                mark_bar(row_new, t[1], t[3]).rehearsal.append(text)
            continue
        if re.fullmatch(r"[12]\.", text):
            target = bar_at(row_new, t[1], 6)
            target.volta = text[0]
            continue
        if text in {"Coda", "To Coda"}:
            continue
        target = bar_at(row_new, t[1], 2)
        target.texts.append(Text(t[1], t[2], [(text, True, True)], above=True, size=9))


def _place_chords(sheet: _Sheet, row_new: list[Bar], row_ch: list[_Glyph], items: list[_Span]) -> None:
    taken: set[int] = set()
    for t in sorted(items, key=operator.itemgetter(1)):
        if id(t) in taken:
            continue
        text = _chord_text(t, row_ch, items, taken)
        chord = parse_chord(text, sheet.prev, t[1])
        if chord is None:
            sheet.flags.append(Issue(row_new[0].number, f"unread chord {text!r}"))
            continue
        target = bar_at(row_new, t[1], 4)
        target.chords.append(chord)
        sheet.prev = chord


def _chord_text(t: _Span, row_ch: list[_Glyph], items: list[_Span], taken: set[int]) -> str:
    text = t[0].strip()
    for c in row_ch:
        if c[0] == CHORD_MAJ7 and 0 < c[1] - t[3] < MAJ7_X_REACH and abs(c[2] - t[2]) < MAJ7_Y_REACH:
            text += "maj7"
    for c in row_ch:
        if c[0] == CHORD_SLASH and 0 < c[1] - t[3] < SLASH_X_REACH:
            follow = next(
                (q for q in items if 0 < q[1] - c[1] < SLASH_BASS_X_REACH and abs(q[2] - t[2]) < SLASH_BASS_Y_REACH),
                None,
            )
            if follow:
                text += "/" + follow[0].strip()
                taken.add(id(follow))
    return text


def _stop_voltas(bars: list[Bar]) -> None:
    for i, bar in enumerate(bars):
        if bar.volta:
            j = i
            if bar.volta == "1":
                while j + 1 < len(bars) and not bars[j].repeat_end:
                    j += 1
            bars[j].volta_stop = bar.volta


def read_bar(bar: Bar, content: list[_Glyph], staff: _Staff) -> list[list[Event]]:
    events: list[Event] = []
    state: dict[tuple[str, int], int] = {}
    content = sorted(content, key=operator.itemgetter(1))
    for glyph in content:
        code, x = glyph[0], glyph[1]
        if code in {BLACK, HALF}:
            events.append(_read_note(glyph, content, staff, state))
        elif code in RESTS:
            events.append(Event(x, [], RESTS[code]))
        elif code == BAR_REST:
            pass
    merged = _merge_chords(events)
    if not merged:
        return [[Event(bar.left, [], bar.length, bar_rest=True)]]
    real = [e for e in merged if not e.grace]
    for _, tx, _, _ in [c for c in content if c[0] == TRIPLET]:
        if not _mark_triplet(real, tx):
            staff.flags.append(Issue(bar.number, "unplaced triplet"))
    t = Fraction(0)
    _check_length(bar, real, staff.flags)
    for e in merged:
        e.onset = t
        t += e.length
    return [merged]


def _read_note(
    glyph: _Glyph,
    content: list[_Glyph],
    staff: _Staff,
    state: dict[tuple[str, int], int],
) -> Event:
    code, x, y, sz = glyph
    grace = sz < GRACE_MAX_SIZE
    near_stems = [s for s in staff.stems if -1 <= s[0] - x <= STEM_X_REACH and s[1] - 1 <= y <= s[2] + 1]
    stem = None
    beam = None
    nominal = Fraction(1) if code == BLACK else Fraction(2)
    if near_stems:
        stem, beam, nominal = _stemmed_value(code, x, near_stems[0], content, staff.fills)
    index = 2 + round((staff.base - y) / staff.step) + 4 * 7
    accidental = _accidental(x, y, content)
    step_name, octave = LETTERS[index % 7], index // 7 - 1
    if accidental is not None:
        state[step_name, octave] = accidental
    head = Head(
        x,
        y,
        step_name,
        octave,
        None,
        "normal",
        alter=state.get((step_name, octave), 1 if step_name == "F" else 0),
    )
    ev = Event(x, [head], Fraction(1, 2) if grace else nominal, 0, stem, beam=beam, grace=grace)
    ev.fermata = any(f[0] in {0xE4C0, 0xE4C1} and abs(f[1] - x) < FERMATA_REACH for f in content)
    ev.accent = any(f[0] in {0xE4A0, 0xE4A1} and abs(f[1] - x) < ACCENT_REACH for f in content)
    return ev


def _stemmed_value(
    code: int,
    x: float,
    st: tuple[float, float, float],
    content: list[_Glyph],
    fills: list[pymupdf.Rect],
) -> tuple[str, int | None, Fraction]:
    nominal = Fraction(1) if code == BLACK else Fraction(2)
    beam = None
    stem = "up" if st[0] - x > STEM_UP_OFFSET else "down"
    tip = st[1] if stem == "up" else st[2]
    hits = [
        r
        for r in fills
        if r.x0 - 0.6 <= st[0] <= r.x1 + 0.6
        and min(r.y0, r.y1) - 1.5 <= tip <= max(r.y0, r.y1) + 1.5
        and r.width > BEAM_MIN_WIDTH
    ]
    level = len({round((r.y0 + r.y1) / 2) for r in hits}) if len(hits) < BEAM_HITS_CAP else 2
    if hits:
        beam = id(hits[0])
    for f in content:
        if f[0] in FLAG_UP and abs(f[1] - st[0]) < FLAG_UP_REACH:
            level = max(level, FLAG_UP[f[0]])
        if f[0] in FLAG_DOWN and abs(f[1] - st[0]) < FLAG_DOWN_REACH:
            level = max(level, FLAG_DOWN[f[0]])
    nominal = nominal / 2**level if code == BLACK else nominal
    return stem, beam, nominal


def _accidental(x: float, y: float, content: list[_Glyph]) -> int | None:
    accidental = None
    for f in content:
        if 0 < x - f[1] < ACCIDENTAL_X_REACH and abs(f[2] - y) < ACCIDENTAL_Y_REACH and f[0] in {0xE260, 0xE261, 0xE262}:
            accidental = {0xE260: -1, 0xE261: 0, 0xE262: 1}[f[0]]
    return accidental


def _merge_chords(events: list[Event]) -> list[Event]:
    events.sort(key=lambda e: e.x)
    merged: list[Event] = []
    for ev in events:
        for other in merged:
            if abs(other.x - ev.x) < CHORD_MERGE_REACH and other.heads and ev.heads and not other.grace and not ev.grace:
                other.heads += ev.heads
                break
        else:
            merged.append(ev)
    for ev in merged:
        ev.heads.sort(key=lambda h: -h.y)
    return merged


def _mark_triplet(real: list[Event], tx: float) -> bool:
    nearest = min(range(len(real)), key=lambda i: abs(real[i].x - tx))
    for span in range(1, 5):
        for lo in range(max(0, nearest - span + 1), nearest + 1):
            window = real[lo : lo + span]
            if (
                len(window) == span
                and not any(e.tuplet for e in window)
                and sum((e.nominal for e in window), Fraction(0)) in {Fraction(3, 2), Fraction(3), Fraction(3, 4)}
                and len(window) > 1
            ):
                for e in window:
                    e.tuplet = True
                return True
    return False


def _check_length(bar: Bar, real: list[Event], flags: list[Issue]) -> None:
    total = sum((e.length for e in real), Fraction(0))
    if total != bar.length:
        flags.append(
            Issue(
                bar.number,
                "bar reads "
                + str(total)
                + ": "
                + " ".join(
                    (("+".join(h.step + str(h.octave) for h in e.heads)) or "rest") + ":" + str(e.length) for e in real
                ),
            ),
        )


def wire_ties(bars: list[Bar], ties: list[pymupdf.Rect]) -> None:
    all_ev = [e for b in bars for v in b.voices for e in v if e.heads]
    for r in ties:
        starts = [
            e
            for e in all_ev
            if abs(e.x + 3 - r.x0) < TIE_START_X_REACH
            and any(abs(h.y - r.y0) < TIE_Y_REACH for h in e.heads)
            and abs((e.x) - r.x0) < TIE_START_MAX_OFFSET
        ]
        rr = [
            e
            for e in all_ev
            if abs(e.x + 2 - r.x1) < TIE_END_X_REACH
            and e.x > r.x0 + 3
            and any(abs(h.y - r.y1) < TIE_Y_REACH for h in e.heads)
        ]
        if starts and rr:
            a = min(starts, key=lambda e: abs(e.x - r.x0))
            b = min(rr, key=lambda e: abs(e.x - r.x1))
            pair = [(p, q) for p in a.heads for q in b.heads if (p.step, p.octave) == (q.step, q.octave)]
            if pair:
                pair[0][0].tie_start = True
                pair[0][1].tie_stop = True
            else:
                a.heads[0].slur_start = True
                b.heads[0].slur_stop = True
