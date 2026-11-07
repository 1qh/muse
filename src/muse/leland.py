"""Decode a MuseScore Studio vector export set in Leland into the score model."""
from __future__ import annotations

import re
from fractions import Fraction as F
from typing import Any

import pymupdf

from muse.model import LETTERS, Bar, Chord, Event, Head, Issue, Score, Text, UnknownGlyph
from muse.symbolfont import parse_chord

WHOLE, HALF, BLACK, CROSS = 0xE0A2, 0xE0A3, 0xE0A4, 0xE0A9
HEADS = {WHOLE: F(4), HALF: F(2), BLACK: F(1), CROSS: F(1)}
RESTS = {0xE4E4: F(2), 0xE4E5: F(1), 0xE4E6: F(1, 2), 0xE4E7: F(1, 4)}
BAR_REST = 0xE4E3
FLAGS = {0xE240: 1, 0xE241: 1, 0xE242: 2, 0xE243: 2}
ACCIDENTALS = {0xE260: -1, 0xE261: 0, 0xE262: 1}
DOT = 0xE1E7
ACCENTS = (0xE4A0, 0xE4A1)
FERMATA = (0xE4C0, 0xE4C1)
TIME_DIGITS = range(0xE080, 0xE08A)
REPEAT_DOT, CLEF, SEGNO, CODA, WIGGLE, TEMPO_NOTE = 0xE044, 0xE050, 0xE047, 0xE048, 0xEAA9, 0xECA5
KNOWN = {*HEADS, *RESTS, BAR_REST, *FLAGS, *ACCIDENTALS, DOT, *ACCENTS, *FERMATA, *TIME_DIGITS, REPEAT_DOT, CLEF, SEGNO, CODA, WIGGLE, TEMPO_NOTE}
SHARP_ORDER, FLAT_ORDER = "FCGDAEB", "BEADGCF"
CREDIT_TOP, CREDIT_BOTTOM = 62.0, 770.0
CHORD_KIND = {"M7": "maj7", "M9": "maj9", "maj": "", "Maj7": "maj7"}
STAFF_REACH = 60.0
DISPLACED = 9.5  # points: a head beside its chord across a second
TUPLET_SIZE = 9.0  # points: an italic digit this large is a tuplet, smaller a bar number


def glyphs_and_text(page: Any) -> tuple[list[tuple[int, float, float]], list[tuple[str, float, float, float, str, float, list[Any]]]]:
    glyphs, spans = [], []
    for block in page.get_text("rawdict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                if span["font"] in ("Leland", "LelandText", "BravuraText", "Bravura"):
                    for ch in span["chars"]:
                        if not ch["c"].isspace():
                            if ord(ch["c"]) not in KNOWN:
                                raise UnknownGlyph(f"music glyph {ord(ch['c']):#x} is not decoded")
                            glyphs.append((ord(ch["c"]), ch["origin"][0], ch["origin"][1]))
                else:
                    text = "".join(c["c"] for c in span["chars"])
                    if text.strip():
                        spans.append((text, span["chars"][0]["origin"][0], span["origin"][1], span["bbox"][2], span["font"], span["size"], span["chars"]))
    return glyphs, spans


def split_chords(span: tuple[str, float, float, float, str, float, list[Any]]) -> list[tuple[str, float]]:
    found, word, at = [], "", 0.0
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
    """Accidentals beside no notehead: a key signature, not an alteration."""
    heads = [g for g in content if g[0] in HEADS]
    return [g for g in content if g[0] in ACCIDENTALS and not any(0 < h[1] - g[1] < 13 and abs(h[2] - g[2]) < 1.6 for h in heads)]


def signature(marks: list[tuple[int, float, float]]) -> int:
    return sum(g[0] == 0xE262 for g in marks) - sum(g[0] == 0xE260 for g in marks)


def alter_in_key(letter: str, key: int) -> int:
    if key > 0 and letter in SHARP_ORDER[:key]:
        return 1
    if key < 0 and letter in FLAT_ORDER[:-key]:
        return -1
    return 0


def stems_of(code: int, x: float, y: float, stems: list[Any]) -> list[Any]:
    """Stems of a head: up on its right edge, down on its left; never a neighbour's."""
    margin = 3.5 if code == CROSS else 1.0
    right = (5.0, 12.0) if code == CROSS else (5.0, 9.5)
    return [s for s in stems if (-1.0 <= s[0] - x <= 1.0 or right[0] <= s[0] - x <= right[1]) and s[1] - margin <= y <= s[2] + margin]


def same_chord(one: Event, other: Event, stem_at: dict[int, float]) -> bool:
    """2 heads of 1 chord: on 1 stem, or stemless and displaced by a second."""
    if not (one.heads and other.heads) or one.nominal != other.nominal or one.heads[0].shape != other.heads[0].shape:
        return False
    if id(one) in stem_at and id(other) in stem_at:
        return abs(stem_at[id(one)] - stem_at[id(other)]) < 0.3
    return id(one) not in stem_at and id(other) not in stem_at and abs(one.x - other.x) < DISPLACED


def set_tuplets(bar: Bar, events: list[Event], marks: list[tuple[float, int]], flags: list[Issue]) -> None:
    """A tuplet of 3 or 6 units covers the nearest run of events filling them, however its beams subdivide them."""
    for at, count in marks:
        best = None
        for lo in range(len(events)):
            for hi in range(lo + 2, len(events) + 1):
                window = events[lo:hi]
                units = sum((e.nominal for e in window), F(0))
                fits = any(units == count * unit for unit in (F(1), F(1, 2), F(1, 4), F(1, 8)))
                if fits and not any(e.tuplet for e in window) and (best is None or abs((window[0].x + window[-1].x) / 2 - at) < abs((best[0].x + best[-1].x) / 2 - at)):
                    best = window
        if best is None:
            flags.append(Issue(bar.number, "unplaced tuplet"))
            continue
        for e in best:
            e.tuplet = True


def read_events(bar: Bar, content: list[tuple[int, float, float]], base: float, step: float, stems: list[Any], beams: list[Any], key: int, flags: list[Issue], marks: list[tuple[float, int]] | None = None) -> list[list[Event]]:
    events: list[Event] = []
    state: dict[tuple[str, int], int] = {}
    stem_at: dict[int, float] = {}
    for code, x, y in sorted(content, key=lambda c: c[1]):
        if code in HEADS:
            near = stems_of(code, x, y, stems)
            nominal, stem, beam = HEADS[code], None, None
            stem_x = None
            if near:
                st = near[0]
                stem_x = st[0]
                stem = "up" if st[0] - x > 3 else "down"
                hits = [b for b in beams if b[0] - 0.8 <= st[0] <= b[1] + 0.8 and st[1] - 1.5 <= beam_y(b, st[0]) <= st[2] + 1.5]
                level = len(hits)
                for f in content:
                    if f[0] in FLAGS and abs(f[1] - st[0]) < 1.5 and st[1] - 8 <= f[2] <= st[2] + 8:
                        level = max(level, FLAGS[f[0]])
                if code in (BLACK, CROSS):
                    nominal = nominal / 2**level
                beam = id(hits[0]) if hits else None
            dots = sum(1 for d in content if d[0] == DOT and 0 < d[1] - x < 22 and abs(d[2] - y) < step * 1.6 and not any(0 < d[1] - o[1] < d[1] - x for o in content if o[0] in HEADS and o[1] > x + 2))
            dots = min(dots, 1)
            index = 2 + round((base - y) / step) + 4 * 7
            letter, octave = LETTERS[index % 7], index // 7
            for f in content:
                if f[0] in ACCIDENTALS and 0 < x - f[1] < 13 and abs(f[2] - y) < 1.6:
                    state[(letter, octave)] = ACCIDENTALS[f[0]]
            head = Head(x, y, letter, octave, None, "x" if code == CROSS else "normal", alter=state.get((letter, octave), alter_in_key(letter, key)))
            event = Event(x, [head], nominal * (F(3, 2) if dots else 1), dots, stem, beam=beam)
            if stem_x is not None:
                stem_at[id(event)] = stem_x
            event.accent = any(f[0] in ACCENTS and abs(f[1] - x) < 8 for f in content)
            event.fermata = any(f[0] in FERMATA and abs(f[1] - x) < 8 for f in content)
            events.append(event)
        elif code in RESTS:
            dotted = any(d[0] == DOT and 0 < d[1] - x < 14 and abs(d[2] - y) < step * 3 for d in content)
            events.append(Event(x, [], RESTS[code] * (F(3, 2) if dotted else 1), 1 if dotted else 0))
    merged: list[Event] = []
    for event in sorted(events, key=lambda e: e.x):
        twin = next((o for o in merged if same_chord(o, event, stem_at)), None)
        if twin:
            twin.heads += event.heads
        else:
            merged.append(event)
    for event in merged:
        event.heads.sort(key=lambda h: -h.y)
    if not merged:
        return [[Event(bar.left, [], bar.length, bar_rest=True)]]
    set_tuplets(bar, merged, marks or [], flags)
    return split_voices(bar, merged, flags)


def beam_y(beam: tuple[float, float, float, float], x: float) -> float:
    """The middle of a slanted beam at x."""
    x0, x1, y0, y1 = beam
    return y0 if x1 - x0 < 0.01 else y0 + (y1 - y0) * (min(max(x, x0), x1) - x0) / (x1 - x0)


def timed(voice: list[Event], length: F) -> bool:
    return sum((e.length for e in voice), F(0)) == length


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
    flags.append(Issue(bar.number, "bar reads " + str(sum((e.length for e in events), F(0))) + " of " + str(bar.length)))
    return [place(events)]


def subsets(items: list[Event]) -> list[list[Event]]:
    return [[e for i, e in enumerate(items) if mask >> i & 1] for mask in range(2 ** min(len(items), 8))]


def place(voice: list[Event]) -> list[Event]:
    at = F(0)
    for event in voice:
        event.onset = at
        at += event.length
    return voice


class Staff:
    """1 printed staff: its lines, the glyphs nearest it, and its bar columns."""

    def __init__(self, top: float, base: float, glyphs: list[tuple[int, float, float]], lines: list[Any], heavy: list[Any], walls: list[Any]) -> None:
        self.top, self.base = top, base
        self.heavy = heavy
        self.step = (base - top) / 8
        self.middle = (top + base) / 2
        self.all = glyphs
        self.glyphs: list[tuple[int, float, float]] = []
        ends = [(a, b) for y, a, b in lines if abs(y - top) < 0.2]
        self.left, self.right = min(a for a, _ in ends), max(b for _, b in ends)
        strokes = sorted({*(round(x, 1) for x, y0, y1, _ in heavy if y0 - 1 <= top and y1 + 1 >= base), *(round((r.x0 + r.x1) / 2, 1) for r in walls if r.height > base - top - 1 and r.y0 - 1 <= top)})
        merged: list[float] = []
        for x in strokes:
            if not merged or x - merged[-1] > 6:
                merged.append(x)
        if not merged or merged[-1] < self.right - 6:
            merged.append(self.right)
        self.cols = [self.left, *merged]

    def claim(self, middles: list[float]) -> None:
        self.glyphs = [g for g in self.all if min(middles, key=lambda m: abs(m - g[2])) == self.middle and abs(g[2] - self.middle) < STAFF_REACH]

    def follows(self, above: Staff) -> bool:
        """A second staff of 1 system: every bar line it has falls on 1 of the staff above, and either the 2 end together or a bar line runs down from the staff above into this one; it may start later and end sooner."""
        mine, theirs = self.cols[1:-1], above.cols[1:-1]
        same_end = abs(self.right - above.right) < 1.5
        joined = any(y0 <= above.base + 1 and y1 >= self.top - 1 for x, y0, y1, _ in self.heavy if any(abs(x - c) < 1.5 for c in mine))
        return bool(mine) and (same_end or joined) and all(any(abs(a - b) < 1.5 for b in theirs) for a in mine)  # a part entering later or leaving sooner keeps the system's barlines


def staff_lines(rules: list[tuple[float, float, float]]) -> list[tuple[float, float, float]]:
    """Each height's longest run of touching strokes over 100pt: a staff line drawn whole or 1 bar at a time; a stack of short ledger lines never touches end to end."""
    found = []
    for y in sorted({round(y, 1) for y, _, _ in rules}):
        pieces = sorted((a, b) for at, a, b in rules if round(at, 1) == y)
        start, end = pieces[0]
        for a, b in pieces[1:]:
            if a - end > 1.0:
                if end - start > 100:
                    found.append((y, start, end))
                start = a
            end = max(end, b)
        if end - start > 100:
            found.append((y, start, end))
    return found


def staff_tops(ys: list[float]) -> list[float]:
    """Each staff's top line: 5 evenly spaced horizontal lines; a stray long line is no staff."""
    tops = []
    at = 0
    while at + 4 < len(ys):
        gaps = [ys[at + i + 1] - ys[at + i] for i in range(4)]
        if max(gaps) - min(gaps) < 0.3 and 3 < min(gaps) < 8:
            tops.append(ys[at])
            at += 5
        else:
            at += 1
    return tops


def decode(path: str) -> Score:
    doc = pymupdf.open(path)
    bars: list[Bar] = []
    flags: list[Issue] = []
    key, length = 0, F(4)
    previous: Chord | None = None
    first_time: tuple[int, int] | None = None
    pending, second = False, False
    all_ties: list[Any] = []
    for page_number, page in enumerate(doc):
        glyphs, spans = glyphs_and_text(page)
        lines, rules, stems, heavy, beams, boxes, walls = [], [], [], [], [], [], []
        for d in page.get_drawings():
            kinds = "".join(i[0] for i in d["items"])
            r = d["rect"]
            if kinds == "l":
                a, b = d["items"][0][1], d["items"][0][2]
                if abs(a.y - b.y) < 0.1:
                    rules.append((a.y, min(a.x, b.x), max(a.x, b.x)))  # every horizontal stroke: a staff drawn 1 bar at a time has short pieces
                if abs(a.x - b.x) < 0.1 and d["width"] > 0.9:
                    heavy.append((a.x, r.y0, r.y1, d["width"]))
                elif abs(a.x - b.x) < 0.1:
                    stems.append((a.x, r.y0, r.y1))
            elif kinds == "llll":
                corners = [p for item in d["items"] for p in (item[1], item[2])]
                low, high = min(p.x for p in corners), max(p.x for p in corners)
                ends = [sum(p.y for p in corners if abs(p.x - at) < 0.1) / max(1, sum(abs(p.x - at) < 0.1 for p in corners)) for at in (low, high)]
                if r.width >= page.rect.width - 1:
                    continue  # the page background
                if r.width >= 4:
                    beams.append((low, high, ends[0], ends[1]))
                else:
                    walls.append(r)
            elif kinds == "cc":
                all_ties.append((page_number, r))
            elif kinds == "qu":
                boxes.append(r)
        lines = staff_lines(rules)
        ys = sorted({round(y, 1) for y, _, _ in lines})
        tops = staff_tops(ys)
        staves = [Staff(top, ys[ys.index(top) + 4], glyphs, rules, heavy, walls) for top in tops]  # long strokes find a staff; every stroke at its height bounds it
        for staff in staves:
            staff.claim([other.middle for other in staves])
        groups: list[list[Staff]] = []
        for staff in staves:
            if groups and staff.follows(groups[-1][-1]):
                groups[-1].append(staff)
            else:
                groups.append([staff])
        owners = text_owners(spans, groups)
        for at_group, group in enumerate(groups):
            staff = group[0]
            row_bars: list[Bar] = []
            for a, b in zip(staff.cols, staff.cols[1:], strict=False):
                content = [g for g in staff.glyphs if a <= g[1] < b - 0.3]
                events = [g for g in content if g[0] in HEADS or g[0] in RESTS or g[0] == BAR_REST]
                free = free_accidentals(content)
                if not events:
                    if free and signature(free) != key:
                        key, pending = signature(free), True
                    continue
                first_x = min(g[1] for g in events)
                digits = [g for g in content if g[0] in TIME_DIGITS and g[1] < first_x]
                changed = None
                if digits:
                    upper = [g for g in digits if g[2] < staff.middle + 0.5]
                    lower = [g for g in digits if g not in upper]
                    changed = (upper[0][0] - 0xE080, lower[0][0] - 0xE080)
                    if first_time is None:
                        first_time = changed
                    length = F(4 * changed[0], changed[1])
                leading = [g for g in free if g[1] < first_x]
                trailing = [g for g in free if g[1] > first_x]
                if leading and signature(leading) != key:
                    key, pending = signature(leading), True
                bar = Bar(len(bars) + 1, page_number, a, b, length)
                bar.new_system = not row_bars and bool(bars)
                bar.new_page = bar.new_system and page_number > 0 and group is groups[0]
                bar.base = staff.base
                bar.time = changed
                if not bars:
                    bar.key, bar.time = key, first_time
                    pending = False
                elif pending:
                    bar.key, pending = key, False
                marks = [(sp[1], int(sp[0].strip())) for sp in spans if sp[4] == "Edwin-Italic" and sp[5] > TUPLET_SIZE and sp[0].strip() in ("3", "6") and a <= sp[1] < b and abs(sp[2] - staff.middle) < STAFF_REACH * 0.7]
                bar.voices = read_events(bar, content, staff.base, staff.step, stems, beams, key, flags, marks)
                if len(group) > 1:
                    lower_content = [g for g in group[1].glyphs if a <= g[1] < b - 0.3 and (g[0] in HEADS or g[0] in RESTS or g[0] in FLAGS or g[0] == DOT or g[0] in ACCIDENTALS)]
                    bar.voices2 = read_events(bar, lower_content, group[1].base, group[1].step, stems, beams, key, flags)
                    second = True
                bars.append(bar)
                row_bars.append(bar)
                if trailing and signature(trailing) != key:
                    key, pending = signature(trailing), True
            if not row_bars:
                continue
            for g in staff.glyphs:
                if g[0] == REPEAT_DOT:
                    near = next((n for n in row_bars if n.left <= g[1] < n.right), None) or min(row_bars, key=lambda n: min(abs(n.left - g[1]), abs(n.right - g[1])))  # the bar the dot sits in: a key change can part a start repeat from the double barline ending the bar before
                    if abs(near.left - g[1]) < abs(near.right - g[1]):
                        near.repeat_start = True
                    else:
                        near.repeat_end = True
                elif g[0] == SEGNO and g[2] < staff.base:
                    next((n for n in row_bars if n.left - 8 <= g[1] < n.right), row_bars[-1]).segno = True
                elif g[0] == CODA and g[2] < staff.base:
                    next((n for n in row_bars if n.left - 8 <= g[1] < n.right), row_bars[-1]).coda = True
            row_text([span for span, owner in zip(spans, owners, strict=True) if owner == at_group], staff.base, staff.top, row_bars, previous, flags, group, boxes)
            for bar in row_bars:
                for chord in bar.chords:
                    previous = chord
    wire(bars, all_ties)
    bars[-1].final = True
    bare_jumps(bars)
    volta_ends(bars)
    tempo(doc, bars)
    return Score(bars, [], flags, second)


TEXT_ABOVE = 40.0  # points above a line's top staff its text window reaches
TEXT_BELOW = 34.0  # points below its bottom staff


def text_owners(spans: list[Any], groups: list[list[Staff]]) -> list[int | None]:
    """The 1 line each text belongs to: in the window of 1 line, that line; where 2 windows overlap, the line whose staff it sits above, since chords, endings and marks print above their staff."""
    owners: list[int | None] = []
    for span in spans:
        y = span[2]
        near = [at for at, group in enumerate(groups) if group[0].top - TEXT_ABOVE < y < group[-1].base + TEXT_BELOW]
        above = [at for at in near if y < groups[at][0].top]
        owners.append(min(above, key=lambda at: groups[at][0].top) if above else (near[0] if near else None))
    return owners


def row_text(spans: list[Any], base: float, top: float, row: list[Bar], previous: Chord | None, flags: list[Issue], group: list[Staff], boxes: list[Any]) -> None:
    bottom = group[-1].base
    for span in spans:
        text = span[0].strip()
        if not (top - TEXT_ABOVE < span[2] < bottom + TEXT_BELOW) or span[2] < CREDIT_TOP or span[2] > CREDIT_BOTTOM:
            continue
        font = span[4]
        target = next((n for n in row if n.left - 4 <= span[1] < n.right - 1), row[-1])
        if font == "Edwin-Italic" and re.fullmatch(r"\d+", text):
            continue
        if font == "Edwin-Roman" and span[2] < top:
            for word, at in split_chords(span):
                chord = parse_chord(word, previous, at)
                if chord is None:
                    flags.append(Issue(row[0].number, f"unread chord {word!r}"))
                    continue
                chord.kind = CHORD_KIND.get(chord.kind, chord.kind)
                holder = next((n for n in row if n.left - 4 <= at < n.right - 1), row[-1])
                holder.chords.append(chord)
                previous = chord
            continue
        if font == "Edwin-Bold" and "=" in text:
            continue
        if re.fullmatch(r"[12]\.", text):
            target.volta = text[0]
            continue
        if font == "Edwin-Bold" and any(b.x0 <= span[1] <= b.x1 and b.y0 <= span[2] <= b.y1 for b in boxes):
            target.rehearsal.append(text)
            continue
        if re.fullmatch(r"[12]\.", text):
            target.volta = text[0]
            continue
        words = "".join(ch["c"] for ch in span[6]).strip()
        low = words.lower()
        if low.startswith("to coda") or low == "coda":
            target.to_coda = low.startswith("to coda") or target.to_coda
            target.coda = low == "coda" or target.coda
            continue
        if low.startswith(("d.s", "dal segno")):
            target.dal_segno = True
            continue
        low_staff = len(group) > 1 and span[2] > group[1].top - 14
        (target.texts2 if low_staff else target.texts).append(Text(span[1], span[2], [(words, "Bold" in font, "Italic" in font)], span[2] < (group[1].base if low_staff else base), size=span[5] * 0.9))


def wire(bars: list[Bar], ties: list[Any]) -> None:
    for page_number, r in ties:
        events = [e for b in bars if b.page == page_number for v in b.voices for e in v if e.heads]
        left = [e for e in events if abs(e.x + 6 - r.x0) < 9 and any(abs(h.y - r.y0) < 12 or abs(h.y - r.y1) < 12 for h in e.heads)]
        right = [e for e in events if abs(e.x - r.x1) < 8 and e.x > r.x0 + 3 and any(abs(h.y - r.y0) < 12 or abs(h.y - r.y1) < 12 for h in e.heads)]
        if left and right:
            a, b = min(left, key=lambda e: abs(e.x - r.x0)), min(right, key=lambda e: abs(e.x - r.x1))
            pair = [(p, q) for p in a.heads for q in b.heads if (p.step, p.octave) == (q.step, q.octave)]
            if pair:
                pair[0][0].tie_start = pair[0][1].tie_stop = True


def bare_jumps(bars: list[Bar]) -> None:
    """A sheet can print signs with no words: the 2nd segno is the jump back, the 1st of 2 codas the jump away."""
    segnos = [b for b in bars if b.segno]
    if len(segnos) == 2:
        segnos[1].segno, segnos[1].dal_segno = False, True
    codas = [b for b in bars if b.coda]
    if len(codas) == 2:
        codas[0].coda, codas[0].to_coda = False, True


def volta_ends(bars: list[Bar]) -> None:
    """An ending stops at its repeat, or at its own bar when no repeat closes it."""
    for at, bar in enumerate(bars):
        if bar.volta:
            stop = at
            if bar.volta == "1":
                while stop + 1 < len(bars) and not bars[stop].repeat_end:
                    stop += 1
            bars[stop].volta_stop = bar.volta


def tempo(doc: Any, bars: list[Bar]) -> None:
    found = re.search(r"=\s*(\d+)", doc[0].get_text())
    if found:
        bars[0].tempo = ("", float(found[1]))
