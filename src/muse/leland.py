"""Decode a MuseScore Studio vector export set in Leland into the score model."""

from __future__ import annotations

import dataclasses
import operator
import re
from fractions import Fraction
from typing import Any

import pymupdf

from muse.model import LETTERS, Bar, Chord, Event, Head, Issue, Score, Text, UnknownGlyphError, bar_at, mark_bar
from muse.smufl import BAR_REST, RESTS, TIME_DIGITS, beam_y
from muse.symbolfont import parse_chord

WHOLE, HALF, BLACK, CROSS = 0xE0A2, 0xE0A3, 0xE0A4, 0xE0A9
HEADS = {WHOLE: Fraction(4), HALF: Fraction(2), BLACK: Fraction(1), CROSS: Fraction(1)}
FLAGS = {0xE240: 1, 0xE241: 1, 0xE242: 2, 0xE243: 2}
ACCIDENTALS = {0xE260: -1, 0xE261: 0, 0xE262: 1}
FLAT_SIGN, SHARP_SIGN = 0xE260, 0xE262
DOT = 0xE1E7
ACCENTS = (0xE4A0, 0xE4A1)
FERMATA = (0xE4C0, 0xE4C1)
REPEAT_DOT, CLEF, SEGNO, CODA, WIGGLE, TEMPO_NOTE = 0xE044, 0xE050, 0xE047, 0xE048, 0xEAA9, 0xECA5
KNOWN = {
    *HEADS,
    *RESTS,
    BAR_REST,
    *FLAGS,
    *ACCIDENTALS,
    DOT,
    *ACCENTS,
    *FERMATA,
    *TIME_DIGITS,
    REPEAT_DOT,
    CLEF,
    SEGNO,
    CODA,
    WIGGLE,
    TEMPO_NOTE,
}
SHARP_ORDER, FLAT_ORDER = "FCGDAEB", "BEADGCF"
CREDIT_TOP, CREDIT_BOTTOM = 62.0, 770.0
CHORD_KIND = {"M7": "maj7", "M9": "maj9", "maj": "", "Maj7": "maj7"}
STAFF_REACH = 60.0
DISPLACED = 9.5
TUPLET_SIZE = 9.0
ACCIDENTAL_REACH = 13
ACCIDENTAL_RISE = 1.6
STEM_UP_OFFSET = 3
FLAG_REACH = 1.5
DOT_REACH = 22
ARTICULATION_REACH = 8
REST_DOT_REACH = 14
STAFF_LINE_MATCH = 0.2
BAR_LINE_MERGE = 6
BAR_LINE_MATCH = 1.5
STAFF_LINE_MIN_LENGTH = 100
LINE_GAP_SPREAD = 0.3
LINE_GAP_MIN, LINE_GAP_MAX = 3, 8
STRAIGHT = 0.1
HEAVY_WIDTH = 0.9
BEAM_MIN_WIDTH = 4
TIE_START_REACH = 9
TIE_HEAD_REACH = 12
TIE_END_REACH = 8
SIGN_PAIR = 2
STEM_MATCH = 0.3

GlyphSpan = tuple[str, float, float, float, str, float, list[Any]]


def _music_glyphs(span: dict[str, Any], glyphs: list[tuple[int, float, float]]) -> None:
    for ch in span["chars"]:
        if not ch["c"].isspace():
            if ord(ch["c"]) not in KNOWN:
                msg = f"music glyph {ord(ch['c']):#x} is not decoded"
                raise UnknownGlyphError(msg)
            glyphs.append((ord(ch["c"]), ch["origin"][0], ch["origin"][1]))


def _text_span(span: dict[str, Any], spans: list[GlyphSpan]) -> None:
    text = "".join(c["c"] for c in span["chars"])
    if text.strip():
        spans.append((
            text,
            span["chars"][0]["origin"][0],
            span["origin"][1],
            span["bbox"][2],
            span["font"],
            span["size"],
            span["chars"],
        ))


def glyphs_and_text(
    page: pymupdf.Page,
) -> tuple[list[tuple[int, float, float]], list[tuple[str, float, float, float, str, float, list[Any]]]]:
    glyphs: list[tuple[int, float, float]] = []
    spans: list[GlyphSpan] = []
    for block in page.get_text("rawdict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                if span["font"] in {"Leland", "LelandText", "BravuraText", "Bravura"}:
                    _music_glyphs(span, glyphs)
                else:
                    _text_span(span, spans)
    return glyphs, spans


def split_chords(span: tuple[str, float, float, float, str, float, list[Any]]) -> list[tuple[str, float]]:
    found: list[tuple[str, float]] = []
    word, at = "", 0.0
    for ch in span[6]:
        if ch["c"].isspace():
            if word:
                found.append((word, at))
            word = ""
        else:
            if not word:
                at = ch["origin"][0]
            word += ch["c"]
    if word:
        found.append((word, at))
    return found


def free_accidentals(content: list[tuple[int, float, float]]) -> list[tuple[int, float, float]]:
    """Accidentals beside no notehead: a key signature, not an alteration.

    Returns:
        The accidentals beside no notehead.

    """
    heads = [g for g in content if g[0] in HEADS]
    return [
        g
        for g in content
        if g[0] in ACCIDENTALS
        and not any(0 < h[1] - g[1] < ACCIDENTAL_REACH and abs(h[2] - g[2]) < ACCIDENTAL_RISE for h in heads)
    ]


def signature(marks: list[tuple[int, float, float]]) -> int:
    return sum(g[0] == SHARP_SIGN for g in marks) - sum(g[0] == FLAT_SIGN for g in marks)


def alter_in_key(letter: str, key: int) -> int:
    if key > 0 and letter in SHARP_ORDER[:key]:
        return 1
    if key < 0 and letter in FLAT_ORDER[:-key]:
        return -1
    return 0


def stems_of(code: int, x: float, y: float, stems: list[Any]) -> list[Any]:
    """Stems of a head: up on its right edge, down on its left; never a neighbour's.

    Returns:
        The stems of the head.

    """
    margin = 3.5 if code == CROSS else 1.0
    right = (5.0, 12.0) if code == CROSS else (5.0, 9.5)
    return [
        s
        for s in stems
        if (-1.0 <= s[0] - x <= 1.0 or right[0] <= s[0] - x <= right[1]) and s[1] - margin <= y <= s[2] + margin
    ]


def same_chord(one: Event, other: Event, stem_at: dict[int, float]) -> bool:
    """2 heads of 1 chord: on 1 stem, or stemless and displaced by a second.

    Returns:
        Whether the 2 heads belong to 1 chord.

    """
    if not (one.heads and other.heads) or one.nominal != other.nominal or one.heads[0].shape != other.heads[0].shape:
        return False
    if id(one) in stem_at and id(other) in stem_at:
        return abs(stem_at[id(one)] - stem_at[id(other)]) < STEM_MATCH
    return id(one) not in stem_at and id(other) not in stem_at and abs(one.x - other.x) < DISPLACED


def set_tuplets(bar: Bar, events: list[Event], marks: list[tuple[float, int]], flags: list[Issue]) -> None:
    """Mark a tuplet of 3 or 6 units over the nearest run of events filling them, however its beams subdivide them."""
    for at, count in marks:
        best = None
        for lo in range(len(events)):
            for hi in range(lo + 2, len(events) + 1):
                window = events[lo:hi]
                units = sum((e.nominal for e in window), Fraction(0))
                fits = any(units == count * unit for unit in (Fraction(1), Fraction(1, 2), Fraction(1, 4), Fraction(1, 8)))
                if (
                    fits
                    and not any(e.tuplet for e in window)
                    and (
                        best is None or abs((window[0].x + window[-1].x) / 2 - at) < abs((best[0].x + best[-1].x) / 2 - at)
                    )
                ):
                    best = window
        if best is None:
            flags.append(Issue(bar.number, "unplaced tuplet"))
            continue
        for e in best:
            e.tuplet = True


@dataclasses.dataclass
class _Reading:
    content: list[tuple[int, float, float]]
    base: float
    step: float
    stems: list[Any]
    beams: list[tuple[float, float, float, float]]
    key: int
    state: dict[tuple[str, int], int] = dataclasses.field(default_factory=dict)
    stem_at: dict[int, float] = dataclasses.field(default_factory=dict)


def _stemmed(
    reading: _Reading,
    code: int,
    x: float,
    st: tuple[float, float, float],
    nominal: Fraction,
) -> tuple[Fraction, str, int | None, float]:
    stem_x = st[0]
    stem = "up" if st[0] - x > STEM_UP_OFFSET else "down"
    hits = [
        b for b in reading.beams if b[0] - 0.8 <= st[0] <= b[1] + 0.8 and st[1] - 1.5 <= beam_y(b, st[0]) <= st[2] + 1.5
    ]
    level: int = len(hits)
    for f in reading.content:
        if f[0] in FLAGS and abs(f[1] - st[0]) < FLAG_REACH and st[1] - 8 <= f[2] <= st[2] + 8:
            level = max(level, FLAGS[f[0]])
    if code in {BLACK, CROSS}:
        nominal /= 2**level
    beam = id(hits[0]) if hits else None
    return nominal, stem, beam, stem_x


def _dot_count(reading: _Reading, x: float, y: float) -> int:
    content = reading.content
    return sum(
        1
        for d in content
        if d[0] == DOT
        and 0 < d[1] - x < DOT_REACH
        and abs(d[2] - y) < reading.step * 1.6
        and not any(0 < d[1] - o[1] < d[1] - x for o in content if o[0] in HEADS and o[1] > x + 2)
    )


def _head_event(reading: _Reading, code: int, x: float, y: float) -> Event:
    content = reading.content
    near = stems_of(code, x, y, reading.stems)
    nominal, stem, beam = HEADS[code], None, None
    stem_x = None
    if near:
        nominal, stem, beam, stem_x = _stemmed(reading, code, x, near[0], nominal)
    dots = _dot_count(reading, x, y)
    dots = min(dots, 1)
    index = 2 + round((reading.base - y) / reading.step) + 4 * 7
    letter, octave = LETTERS[index % 7], index // 7
    for f in content:
        if f[0] in ACCIDENTALS and 0 < x - f[1] < ACCIDENTAL_REACH and abs(f[2] - y) < ACCIDENTAL_RISE:
            reading.state[letter, octave] = ACCIDENTALS[f[0]]
    head = Head(
        x,
        y,
        letter,
        octave,
        None,
        "x" if code == CROSS else "normal",
        alter=reading.state.get((letter, octave), alter_in_key(letter, reading.key)),
    )
    event = Event(x, [head], nominal * (Fraction(3, 2) if dots else 1), dots, stem, beam=beam)
    if stem_x is not None:
        reading.stem_at[id(event)] = stem_x
    event.accent = any(f[0] in ACCENTS and abs(f[1] - x) < ARTICULATION_REACH for f in content)
    event.fermata = any(f[0] in FERMATA and abs(f[1] - x) < ARTICULATION_REACH for f in content)
    return event


def _rest_event(reading: _Reading, code: int, x: float, y: float) -> Event:
    dotted = any(
        d[0] == DOT and 0 < d[1] - x < REST_DOT_REACH and abs(d[2] - y) < reading.step * 3 for d in reading.content
    )
    return Event(x, [], RESTS[code] * (Fraction(3, 2) if dotted else 1), 1 if dotted else 0)


def _merge_chords(events: list[Event], stem_at: dict[int, float]) -> list[Event]:
    merged: list[Event] = []
    for event in sorted(events, key=lambda e: e.x):
        twin = next((o for o in merged if same_chord(o, event, stem_at)), None)
        if twin:
            twin.heads += event.heads
        else:
            merged.append(event)
    for event in merged:
        event.heads.sort(key=lambda h: -h.y)
    return merged


def read_events(
    bar: Bar,
    content: list[tuple[int, float, float]],
    base: float,
    step: float,
    stems: list[Any],
    beams: list[Any],
    key: int,
    flags: list[Issue],
    marks: list[tuple[float, int]] | None = None,
) -> list[list[Event]]:
    reading = _Reading(content, base, step, stems, beams, key)
    events: list[Event] = []
    for code, x, y in sorted(content, key=operator.itemgetter(1)):
        if code in HEADS:
            events.append(_head_event(reading, code, x, y))
        elif code in RESTS:
            events.append(_rest_event(reading, code, x, y))
    merged = _merge_chords(events, reading.stem_at)
    if not merged:
        return [[Event(bar.left, [], bar.length, bar_rest=True)]]
    set_tuplets(bar, merged, marks or [], flags)
    return split_voices(bar, merged, flags)


def timed(voice: list[Event], length: Fraction) -> bool:
    return sum((e.length for e in voice), Fraction(0)) == length


def split_voices(bar: Bar, events: list[Event], flags: list[Issue]) -> list[list[Event]]:
    if timed(events, bar.length):
        return [place(events)]
    crossed = [e for e in events if e.heads and e.heads[0].shape == "x"]
    if crossed:
        top = [e for e in events if e not in crossed and not (not e.heads and e.x in [c.x for c in crossed])]
        for rests in subsets([e for e in top if not e.heads]):
            first = [e for e in top if e not in rests]
            second = sorted([*crossed, *rests], key=lambda e: e.x)
            if timed(first, bar.length) and timed(second, bar.length):
                return [place(first), place(second)]
    up = [e for e in events if e.stem == "up"]
    down = [e for e in events if e.stem == "down"]
    rests = [e for e in events if not e.heads]
    for chosen in subsets(rests):
        first = sorted([*up, *chosen], key=lambda e: e.x)
        second = sorted([*down, *[r for r in rests if r not in chosen]], key=lambda e: e.x)
        if first and second and timed(first, bar.length) and timed(second, bar.length):
            return [place(first), place(second)]
    flags.append(
        Issue(bar.number, "bar reads " + str(sum((e.length for e in events), Fraction(0))) + " of " + str(bar.length)),
    )
    return [place(events)]


def subsets(items: list[Event]) -> list[list[Event]]:
    return [[e for i, e in enumerate(items) if mask >> i & 1] for mask in range(2 ** min(len(items), 8))]


def place(voice: list[Event]) -> list[Event]:
    at = Fraction(0)
    for event in voice:
        event.onset = at
        at += event.length
    return voice


@dataclasses.dataclass
class _Drawn:
    rules: list[tuple[float, float, float]] = dataclasses.field(default_factory=list)
    stems: list[tuple[float, float, float]] = dataclasses.field(default_factory=list)
    heavy: list[tuple[float, float, float, float]] = dataclasses.field(default_factory=list)
    beams: list[tuple[float, float, float, float]] = dataclasses.field(default_factory=list)
    boxes: list[pymupdf.Rect] = dataclasses.field(default_factory=list)
    walls: list[pymupdf.Rect] = dataclasses.field(default_factory=list)
    ties: list[tuple[int, pymupdf.Rect]] = dataclasses.field(default_factory=list)


class Staff:
    """1 printed staff: its lines, the glyphs nearest it, and its bar columns."""

    def __init__(
        self,
        top: float,
        base: float,
        glyphs: list[tuple[int, float, float]],
        drawn: _Drawn,
    ) -> None:
        lines, heavy, walls = drawn.rules, drawn.heavy, drawn.walls
        self.top, self.base = top, base
        self.heavy = heavy
        self.step = (base - top) / 8
        self.middle = (top + base) / 2
        self.all = glyphs
        self.glyphs: list[tuple[int, float, float]] = []
        ends = [(a, b) for y, a, b in lines if abs(y - top) < STAFF_LINE_MATCH]
        self.left, self.right = min(a for a, _ in ends), max(b for _, b in ends)
        strokes = sorted({
            *(round(x, 1) for x, y0, y1, _ in heavy if y0 - 1 <= top and y1 + 1 >= base),
            *(round((r.x0 + r.x1) / 2, 1) for r in walls if r.height > base - top - 1 and r.y0 - 1 <= top),
        })
        merged: list[float] = []
        for x in strokes:
            if not merged or x - merged[-1] > BAR_LINE_MERGE:
                merged.append(x)
        if not merged or merged[-1] < self.right - BAR_LINE_MERGE:
            merged.append(self.right)
        self.cols = [self.left, *merged]

    def claim(self, middles: list[float]) -> None:
        self.glyphs = [
            g
            for g in self.all
            if min(middles, key=lambda m: abs(m - g[2])) == self.middle and abs(g[2] - self.middle) < STAFF_REACH
        ]

    def follows(self, above: Staff) -> bool:
        """Tell whether this staff is the second staff of 1 system.

        Every bar line it has falls on 1 of the staff above, and either the 2 end together or a bar line runs
        down from the staff above into this one; it may start later and end sooner.

        Returns:
            Whether this staff continues the system of the staff above.

        """
        mine, theirs = self.cols[1:-1], above.cols[1:-1]
        same_end = abs(self.right - above.right) < BAR_LINE_MATCH
        joined = any(
            y0 <= above.base + 1 and y1 >= self.top - 1
            for x, y0, y1, _ in self.heavy
            if any(abs(x - c) < BAR_LINE_MATCH for c in mine)
        )
        return (
            bool(mine) and bool(same_end or joined) and all(any(abs(a - b) < BAR_LINE_MATCH for b in theirs) for a in mine)
        )


def staff_lines(rules: list[tuple[float, float, float]]) -> list[tuple[float, float, float]]:
    """Each height's longest run of touching strokes over 100pt.

    A staff line is drawn whole or 1 bar at a time; a stack of short ledger lines never touches end to end.

    Returns:
        The height, start and end of each staff line.

    """
    found = []
    for y in sorted({round(y, 1) for y, _, _ in rules}):
        pieces = sorted((a, b) for at, a, b in rules if round(at, 1) == y)
        start, end = pieces[0]
        for a, b in pieces[1:]:
            if a - end > 1.0:
                if end - start > STAFF_LINE_MIN_LENGTH:
                    found.append((y, start, end))
                start = a
            end = max(end, b)
        if end - start > STAFF_LINE_MIN_LENGTH:
            found.append((y, start, end))
    return found


def staff_tops(ys: list[float]) -> list[float]:
    """Each staff's top line: 5 evenly spaced horizontal lines; a stray long line is no staff.

    Returns:
        The top line height of each staff.

    """
    tops = []
    at = 0
    while at + 4 < len(ys):
        gaps = [ys[at + i + 1] - ys[at + i] for i in range(4)]
        if max(gaps) - min(gaps) < LINE_GAP_SPREAD and LINE_GAP_MIN < min(gaps) < LINE_GAP_MAX:
            tops.append(ys[at])
            at += 5
        else:
            at += 1
    return tops


@dataclasses.dataclass
class _Sheet:
    bars: list[Bar] = dataclasses.field(default_factory=list)
    flags: list[Issue] = dataclasses.field(default_factory=list)
    boxes: list[pymupdf.Rect] = dataclasses.field(default_factory=list)
    key: int = 0
    length: Fraction = dataclasses.field(default_factory=lambda: Fraction(4))
    previous: Chord | None = None
    first_time: tuple[int, int] | None = None
    pending: bool = False
    second: bool = False


@dataclasses.dataclass
class _Page:
    number: int
    spans: list[GlyphSpan]
    owners: list[int | None]
    groups: list[list[Staff]]
    drawn: _Drawn


def _line(d: dict[str, Any], drawn: _Drawn) -> None:
    r = d["rect"]
    a, b = d["items"][0][1], d["items"][0][2]
    if abs(a.y - b.y) < STRAIGHT:
        drawn.rules.append((a.y, min(a.x, b.x), max(a.x, b.x)))
    if abs(a.x - b.x) < STRAIGHT and d["width"] > HEAVY_WIDTH:
        drawn.heavy.append((a.x, r.y0, r.y1, d["width"]))
    elif abs(a.x - b.x) < STRAIGHT:
        drawn.stems.append((a.x, r.y0, r.y1))


def _quad(d: dict[str, Any], page: pymupdf.Page, drawn: _Drawn) -> None:
    r = d["rect"]
    corners = [p for item in d["items"] for p in (item[1], item[2])]
    low, high = min(p.x for p in corners), max(p.x for p in corners)
    ends = [
        sum(p.y for p in corners if abs(p.x - at) < STRAIGHT) / max(1, sum(abs(p.x - at) < STRAIGHT for p in corners))
        for at in (low, high)
    ]
    if r.width >= page.rect.width - 1:
        return
    if r.width >= BEAM_MIN_WIDTH:
        drawn.beams.append((low, high, ends[0], ends[1]))
    else:
        drawn.walls.append(r)


def _read_drawings(page: pymupdf.Page, page_number: int) -> _Drawn:
    drawn = _Drawn()
    for d in page.get_drawings():
        kinds = "".join(i[0] for i in d["items"])
        r = d["rect"]
        if kinds == "l":
            _line(d, drawn)
        elif kinds == "llll":
            _quad(d, page, drawn)
        elif kinds == "cc":
            drawn.ties.append((page_number, r))
        elif kinds == "qu":
            drawn.boxes.append(r)
    return drawn


def _group_staves(staves: list[Staff]) -> list[list[Staff]]:
    groups: list[list[Staff]] = []
    for staff in staves:
        if groups and staff.follows(groups[-1][-1]):
            groups[-1].append(staff)
        else:
            groups.append([staff])
    return groups


def _key_change(marks: list[tuple[int, float, float]], sheet: _Sheet) -> None:
    if marks and signature(marks) != sheet.key:
        sheet.key, sheet.pending = signature(marks), True


def _time_change(
    content: list[tuple[int, float, float]],
    first_x: float,
    staff: Staff,
    sheet: _Sheet,
) -> tuple[int, int] | None:
    digits = [g for g in content if g[0] in TIME_DIGITS and g[1] < first_x]
    changed = None
    if digits:
        upper = [g for g in digits if g[2] < staff.middle + 0.5]
        lower = [g for g in digits if g not in upper]
        changed = (upper[0][0] - 0xE080, lower[0][0] - 0xE080)
        if sheet.first_time is None:
            sheet.first_time = changed
        sheet.length = Fraction(4 * changed[0], changed[1])
    return changed


def _tuplet_marks(spans: list[GlyphSpan], a: float, b: float, staff: Staff) -> list[tuple[float, int]]:
    return [
        (sp[1], int(sp[0].strip()))
        for sp in spans
        if sp[4] == "Edwin-Italic"
        and sp[5] > TUPLET_SIZE
        and sp[0].strip() in {"3", "6"}
        and a <= sp[1] < b
        and abs(sp[2] - staff.middle) < STAFF_REACH * 0.7
    ]


def _lower_voices(bar: Bar, group: list[Staff], drawn: _Drawn, sheet: _Sheet) -> None:
    a, b = bar.left, bar.right
    lower_content = [
        g
        for g in group[1].glyphs
        if a <= g[1] < b - 0.3 and (g[0] in HEADS or g[0] in RESTS or g[0] in FLAGS or g[0] == DOT or g[0] in ACCIDENTALS)
    ]
    bar.voices2 = read_events(
        bar,
        lower_content,
        group[1].base,
        group[1].step,
        drawn.stems,
        drawn.beams,
        sheet.key,
        sheet.flags,
    )
    sheet.second = True


def _decode_bar(page: _Page, group: list[Staff], cols: tuple[float, float], row_bars: list[Bar], sheet: _Sheet) -> None:
    staff = group[0]
    a, b = cols
    content = [g for g in staff.glyphs if a <= g[1] < b - 0.3]
    events = [g for g in content if g[0] in HEADS or g[0] in RESTS or g[0] == BAR_REST]
    free = free_accidentals(content)
    if not events:
        _key_change(free, sheet)
        return
    first_x = min(g[1] for g in events)
    changed = _time_change(content, first_x, staff, sheet)
    leading = [g for g in free if g[1] < first_x]
    trailing = [g for g in free if g[1] > first_x]
    _key_change(leading, sheet)
    bar = Bar(len(sheet.bars) + 1, page.number, a, b, sheet.length)
    bar.new_system = not row_bars and bool(sheet.bars)
    bar.new_page = bar.new_system and page.number > 0 and group is page.groups[0]
    bar.base = staff.base
    bar.time = changed
    if not sheet.bars:
        bar.key, bar.time = sheet.key, sheet.first_time
        sheet.pending = False
    elif sheet.pending:
        bar.key, sheet.pending = sheet.key, False
    marks = _tuplet_marks(page.spans, a, b, staff)
    drawn = page.drawn
    bar.voices = read_events(bar, content, staff.base, staff.step, drawn.stems, drawn.beams, sheet.key, sheet.flags, marks)
    if len(group) > 1:
        _lower_voices(bar, group, drawn, sheet)
    sheet.bars.append(bar)
    row_bars.append(bar)
    _key_change(trailing, sheet)


def _mark_signs(staff: Staff, row_bars: list[Bar]) -> None:
    for g in staff.glyphs:
        if g[0] == REPEAT_DOT:
            near = next((n for n in row_bars if n.left <= g[1] < n.right), None) or min(
                row_bars,
                key=lambda n: min(abs(n.left - g[1]), abs(n.right - g[1])),
            )
            if abs(near.left - g[1]) < abs(near.right - g[1]):
                near.repeat_start = True
            else:
                near.repeat_end = True
        elif g[0] == SEGNO and g[2] < staff.base:
            bar_at(row_bars, g[1], 8).segno = True
        elif g[0] == CODA and g[2] < staff.base:
            bar_at(row_bars, g[1], 8).coda = True


def _decode_row(page: _Page, at_group: int, group: list[Staff], sheet: _Sheet) -> None:
    staff = group[0]
    row_bars: list[Bar] = []
    for cols in zip(staff.cols, staff.cols[1:], strict=False):
        _decode_bar(page, group, cols, row_bars, sheet)
    if not row_bars:
        return
    _mark_signs(staff, row_bars)
    row_text(
        [span for span, owner in zip(page.spans, page.owners, strict=True) if owner == at_group],
        staff.base,
        row_bars,
        group,
        sheet,
    )
    for bar in row_bars:
        for chord in bar.chords:
            sheet.previous = chord


def _decode_page(page: pymupdf.Page, page_number: int, sheet: _Sheet, all_ties: list[Any]) -> None:
    glyphs, spans = glyphs_and_text(page)
    drawn = _read_drawings(page, page_number)
    all_ties.extend(drawn.ties)
    sheet.boxes = drawn.boxes
    lines = staff_lines(drawn.rules)
    ys = sorted({round(y, 1) for y, _, _ in lines})
    tops = staff_tops(ys)
    staves = [Staff(top, ys[ys.index(top) + 4], glyphs, drawn) for top in tops]
    for staff in staves:
        staff.claim([other.middle for other in staves])
    groups = _group_staves(staves)
    owners = text_owners(spans, groups)
    context = _Page(page_number, spans, owners, groups, drawn)
    for at_group, group in enumerate(groups):
        _decode_row(context, at_group, group, sheet)


def decode(path: str) -> Score:
    doc = pymupdf.open(path)
    sheet = _Sheet()
    all_ties: list[Any] = []
    for page_number in range(doc.page_count):
        _decode_page(doc[page_number], page_number, sheet, all_ties)
    bars = sheet.bars
    wire(bars, all_ties)
    bars[-1].final = True
    bare_jumps(bars)
    volta_ends(bars)
    tempo(doc, bars)
    return Score(bars, [], sheet.flags, sheet.second)


TEXT_ABOVE = 40.0
TEXT_BELOW = 34.0


LYRIC_FONT = "FreeSerif"


def is_lyric(font: str) -> bool:
    """Tell a lyric hint, known by the lyric font it is set in.

    Returns:
        Whether the font is the lyric font.

    """
    return font.rsplit("+", maxsplit=1)[-1].startswith(LYRIC_FONT)


def text_owners(spans: list[Any], groups: list[list[Staff]]) -> list[int | None]:
    """Find the 1 line each text belongs to.

    In the window of 1 line, that line; where 2 windows overlap, a lyric hint to the line it sits under and
    anything else to the line it sits above.

    Returns:
        The index of the owning line of each text, or None for a text in no line's window.

    """
    owners: list[int | None] = []
    for span in spans:
        y = span[2]
        near = [at for at, group in enumerate(groups) if group[0].top - TEXT_ABOVE < y < group[-1].base + TEXT_BELOW]
        if not near:
            owners.append(None)
            continue
        if is_lyric(span[4]):
            under = [at for at in near if y > groups[at][-1].base]
            owners.append(max(under, key=lambda at: groups[at][-1].base) if under else near[0])
        else:
            above = [at for at in near if y < groups[at][0].top]
            owners.append(min(above, key=lambda at: groups[at][0].top) if above else near[0])
    return owners


def _read_chords(span: GlyphSpan, row: list[Bar], previous: Chord | None, flags: list[Issue]) -> Chord | None:
    for word, at in split_chords(span):
        chord = parse_chord(word, previous, at)
        if chord is None:
            flags.append(Issue(row[0].number, f"unread chord {word!r}"))
            continue
        chord.kind = CHORD_KIND.get(chord.kind, chord.kind)
        holder = bar_at(row, at, 4)
        holder.chords.append(chord)
        previous = chord
    return previous


def _read_jump(low: str, target: Bar) -> bool:
    if low.startswith("to coda") or low == "coda":
        target.to_coda = low.startswith("to coda") or target.to_coda
        target.coda = low == "coda" or target.coda
        return True
    if low.startswith(("d.s", "dal segno")):
        target.dal_segno = True
        return True
    return False


def _add_text(span: GlyphSpan, words: str, base: float, target: Bar, group: list[Staff]) -> None:
    font = span[4]
    low_staff = len(group) > 1 and (span[2] > group[1].base if is_lyric(font) else span[2] > group[1].top - 14)
    (target.texts2 if low_staff else target.texts).append(
        Text(
            span[1],
            span[2],
            [(words, "Bold" in font, "Italic" in font)],
            span[2] < (group[1].base if low_staff else base),
            size=span[5] * 0.9,
        ),
    )


def row_text(
    spans: list[Any],
    base: float,
    row: list[Bar],
    group: list[Staff],
    sheet: _Sheet,
) -> None:
    top = group[0].top
    previous, flags, boxes = sheet.previous, sheet.flags, sheet.boxes
    bottom = group[-1].base
    for span in spans:
        text = span[0].strip()
        if not (top - TEXT_ABOVE < span[2] < bottom + TEXT_BELOW) or span[2] < CREDIT_TOP or span[2] > CREDIT_BOTTOM:
            continue
        font = span[4]
        target = bar_at(row, span[1], 4)
        if font == "Edwin-Italic" and re.fullmatch(r"\d+", text):
            continue
        if font == "Edwin-Roman" and span[2] < top:
            previous = _read_chords(span, row, previous, flags)
            continue
        if font == "Edwin-Bold" and "=" in text:
            continue
        if re.fullmatch(r"[12]\.", text):
            target.volta = text[0]
            continue
        box = next((b for b in boxes if b.x0 - 1 <= span[1] <= b.x1 and b.y0 <= span[2] <= b.y1 + 1), None)
        if box is not None and "Bold" in font:
            mark_bar(row, box.x0, box.x1).rehearsal.append(text)
            continue
        words = "".join(ch["c"] for ch in span[6]).strip()
        if _read_jump(words.lower(), target):
            continue
        _add_text(span, words, base, target, group)


def wire(bars: list[Bar], ties: list[Any]) -> None:
    for page_number, r in ties:
        events = [e for b in bars if b.page == page_number for v in b.voices for e in v if e.heads]
        left = [
            e
            for e in events
            if abs(e.x + 6 - r.x0) < TIE_START_REACH
            and any(abs(h.y - r.y0) < TIE_HEAD_REACH or abs(h.y - r.y1) < TIE_HEAD_REACH for h in e.heads)
        ]
        right = [
            e
            for e in events
            if abs(e.x - r.x1) < TIE_END_REACH
            and e.x > r.x0 + 3
            and any(abs(h.y - r.y0) < TIE_HEAD_REACH or abs(h.y - r.y1) < TIE_HEAD_REACH for h in e.heads)
        ]
        if left and right:
            a, b = min(left, key=lambda e: abs(e.x - r.x0)), min(right, key=lambda e: abs(e.x - r.x1))
            pair = [(p, q) for p in a.heads for q in b.heads if (p.step, p.octave) == (q.step, q.octave)]
            if pair:
                pair[0][0].tie_start = pair[0][1].tie_stop = True


def bare_jumps(bars: list[Bar]) -> None:
    """Read signs a sheet prints with no words: the 2nd segno is the jump back, the 1st of 2 codas the jump away."""
    segnos = [b for b in bars if b.segno]
    if len(segnos) == SIGN_PAIR:
        segnos[1].segno, segnos[1].dal_segno = False, True
    codas = [b for b in bars if b.coda]
    if len(codas) == SIGN_PAIR:
        codas[0].coda, codas[0].to_coda = False, True


def volta_ends(bars: list[Bar]) -> None:
    """Stop an ending at its repeat, or at its own bar when no repeat closes it."""
    for at, bar in enumerate(bars):
        if bar.volta is not None and bar.volta:
            stop = at
            if bar.volta == "1":
                while stop + 1 < len(bars) and not bars[stop].repeat_end:
                    stop += 1
            bars[stop].volta_stop = bar.volta


def tempo(doc: pymupdf.Document, bars: list[Bar]) -> None:
    found = re.search(r"=\s*(\d+)", doc[0].get_text())
    if found:
        bars[0].tempo = ("", float(found[1]))
