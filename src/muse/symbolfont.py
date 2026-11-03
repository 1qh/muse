"""Decode the symbol-font symbol encoding from a vector PDF into the score model."""
from __future__ import annotations

import dataclasses
import itertools
import re
from fractions import Fraction as F
from typing import Any

import pymupdf

from muse.model import LETTERS, Bar, Chord, Event, Head, Issue, Score, Text, UnknownGlyph

HEADS = {0xF0CF: ("black", "down"), 0xF0F6: ("black", "up"), 0xF0F3: ("black", "up"), 0xF0F5: ("half", "down"), 0xF0FA: ("half", "up"),
         0xF077: ("whole", None), 0xF0C0: ("x", "up"), 0xF0E1: ("diamond", "up"), 0xF0EB: ("circlex", "up"), 0xF0E2: ("filleddiamond", "down")}
GRACE = 0xF0F7  # slashed grace note drawn as 1 smaller glyph
DOUBLE_TOP = 0x6D  # zigzag cap of a double bar line, nothing heard
TEMPO_EIGHTH = 0x65  # note of a second equivalent tempo mark, the quarter value is the one kept
RESTS = {0xF0E4: F(1, 2), 0xF0C5: F(1, 4), 0xF0CE: F(1), 0xF0EE: F(2)}
CLEF = 0xF026
BAR_REST, SIMILE, STAFF, DOT = 0xF0B7, 0xF022, 0xF03D, 0xF02E
BARS = {0xF06C, 0xF07B, 0xF0D2, 0xF0D3}
FLAGS = {0xF06A: 1, 0xF06B: 2, 0xF04A: 1, 0xF04B: 2}
KNOWN_GLYPHS = frozenset((0x20, 0x23, 0x65, 0x6D, 0xF0E2, 0xF0F7, 0x2E, 0x31, 0x32, 0x33, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39, 0x61, 0x62, 0x69, 0x71, 0x74, 0x75, 0x76, 0xA3, 0xF022, 0xF023, 0xF025, 0xF026, 0xF02E, 0xF03D,
                          0xF03E, 0xF043, 0xF04A, 0xF04B, 0xF055, 0xF05F, 0xF062, 0xF06A, 0xF06B, 0xF06C, 0xF06E, 0xF077, 0xF07B, 0xF0B7, 
                          0xF0C0, 0xF0C5, 0xF0C7, 0xF0C8, 0xF0CE, 0xF0CF, 0xF0D2, 0xF0D3, 0xF0D9, 0xF0DE, 0xF0E1, 0xF0E4, 0xF0EB, 0xF0EE, 0xF0F3, 0xF0F5, 0xF0F6, 0xF0FA))
FLATS, SHARPS, NATURALS = {0x62, 0xF062}, {0x23, 0xF023}, {0x6E, 0xF06E}
STEP_PER_SIZE = 2.16 / 17.9
CREDIT_CLEARANCE = 42.0  # points above the first staff line
CREDIT_X = 300.0
COINCIDENT_X = 2.0
MAX_SPLIT_RESTS = 7
BEAM_GAP = 0.9
MAX_SPLIT_EVENTS = 14
REPAIR_LENGTHS = (F(1, 4), F(1), F(3, 2), F(2), F(3), F(4), F(3, 4), F(1, 2))
OPENING_BAR_MIN = 45.0  # a clef this far before the first bar line has music between them
MARK_Y = 40.0
NOISE = ("Sáng tác", "Trình bày", "Lời Việt", "Singer", "Copyright", "Contact")  # credit labels; the engraver mark sits above the first staff


@dataclasses.dataclass
class G:
    code: int
    x: float
    y: float
    size: float


@dataclasses.dataclass
class Span:
    text: str
    x: float
    y: float
    x1: float
    font: str
    size: float
    bold: bool
    italic: bool


def font_map(doc: pymupdf.Document) -> dict[str, str]:
    """Printed PDFs lose font names; recover each role from its glyph usage."""
    counts: dict[str, dict[str, int]] = {}
    for page in doc:
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                for s in line["spans"]:
                    d = counts.setdefault(s["font"], {"pua": 0, "n": 0})
                    d["n"] += len(s["text"])
                    d["pua"] += sum(ord(c) >= 0xF000 for c in s["text"])
    return {f: "music" for f, d in counts.items() if d["pua"] > 20}


def load(page: pymupdf.Page, music: dict[str, str]) -> tuple[list[G], list[Span]]:
    glyphs: list[G] = []
    spans: list[Span] = []
    for block in page.get_text("rawdict")["blocks"]:
        for line in block.get("lines", []):
            for s in line["spans"]:
                if s["font"] in music:
                    for ch in s["chars"]:
                        if ord(ch["c"]) not in KNOWN_GLYPHS:
                            raise UnknownGlyph(f"music glyph {ord(ch['c']):#x} is not decoded")
                        glyphs.append(G(ord(ch["c"]), ch["origin"][0], ch["origin"][1], s["size"]))
                else:
                    text = "".join(c["c"] for c in s["chars"])
                    if text.strip():
                        flags = s["flags"]
                        name = s["font"].lower()
                        spans.append(Span(text, s["chars"][0]["origin"][0], s["origin"][1], s["bbox"][2], s["font"], s["size"],
                                          bool(flags & 16) or "bold" in name, bool(flags & 2) or "italic" in name))
    return glyphs, spans


ROOT_RE = re.compile(r"^[A-G]")
REHEARSAL = re.compile(r"Intro|Interlude|Vinahouse|[A-C]2?")


def assemble_chords(items: list[tuple[float, float, str]]) -> list[tuple[float, str]]:
    """items are (x, x_end, text) of 1 chord row, left to right."""
    chords: list[tuple[float, str]] = []
    cur: list[str] = []
    cur_x = 0.0
    end = 0.0
    for x, x1, text in sorted(items):
        starts = bool(ROOT_RE.match(text)) or (text.startswith("/") and x - end > 3)
        if cur and starts and not (text[0] in "ABCDEFG" and cur[-1].endswith("/") and x - end < 3):
            chords.append((cur_x, "".join(cur)))
            cur = []
        if not cur:
            cur_x = x
        cur.append(text)
        end = x1
    if cur:
        chords.append((cur_x, "".join(cur)))
    return chords


def parse_chord(text: str, previous: Chord | None, x: float) -> Chord | None:
    text = text.replace(" ", "").replace("♭", "b").replace("♯", "#")
    if text.startswith("/") and previous:
        m = re.match(r"/([A-G])([b#]?)$", text)
        if not m:
            return None
        return Chord(x, previous.root, previous.root_alter, previous.kind, m[1], {"b": -1, "#": 1, "": 0}[m[2]])
    m = re.match(r"([A-G])([b#]?)(.*?)(?:/([A-G])([b#]?))?$", text)
    if not m:
        return None
    alter = {"b": -1, "#": 1, "": 0}
    return Chord(x, m[1], alter[m[2]], m[3], m[4], alter[m[5] or ""])


def voice_split(chords: list[Event], close: list[tuple[Event, Event]], length: F) -> tuple[list[Event], list[Event]] | None:
    """2 voices keeping coincident events apart: 1 fills the bar and the other does not overflow."""
    for mask in range(1, 2 ** len(chords) - 1):
        a = [e for i, e in enumerate(chords) if mask >> i & 1]
        b = [e for i, e in enumerate(chords) if not mask >> i & 1]
        if any((x in a) == (y in a) for x, y in close):
            continue
        s_a, s_b = sum((e.length for e in a), F(0)), sum((e.length for e in b), F(0))
        if s_a == length and s_b == length:
            return a, b
    for mask in range(1, 2 ** len(chords) - 1):
        a = [e for i, e in enumerate(chords) if mask >> i & 1]
        b = [e for i, e in enumerate(chords) if not mask >> i & 1]
        if any((x in a) == (y in a) for x, y in close):
            continue
        s_a, s_b = sum((e.length for e in a), F(0)), sum((e.length for e in b), F(0))
        if s_a == length and s_b <= length:
            return a, b
    return None


def stem_split(chords: list[Event], length: F) -> tuple[list[Event], list[Event]] | None:
    """2 voices by stem direction, each rest going to either voice or, if stamped under a note, to neither."""
    up = [e for e in chords if e.heads and e.stem == "up"]
    down = [e for e in chords if e.heads and e.stem == "down"]
    rests = [e for e in chords if not e.heads]
    if not up or not down or len(up) + len(down) + len(rests) != len(chords) or len(rests) > MAX_SPLIT_RESTS:
        return None
    best = None
    for choice in itertools.product((0, 1, 2), repeat=len(rests)):
        a = up + [r for r, c in zip(rests, choice, strict=True) if c == 0]
        b = down + [r for r, c in zip(rests, choice, strict=True) if c == 1]
        dropped = [r for r, c in zip(rests, choice, strict=True) if c == 2]
        if any(not any(abs(r.x - e.x) < COINCIDENT_X for e in chords if e.heads) for r in dropped):
            continue
        if sum((e.length for e in a), F(0)) == length and sum((e.length for e in b), F(0)) == length and (best is None or len(dropped) < best[0]):
            best = (len(dropped), sorted(a, key=lambda e: e.x), sorted(b, key=lambda e: e.x))
    return (best[1], best[2]) if best else None


def beam_levels(centres: list[float]) -> int:
    """Beams drawn in pieces share a height; only a real gap makes another level."""
    levels = 0
    last = -100.0
    for y in sorted(centres):
        if y - last > BEAM_GAP:
            levels += 1
            last = y
    return levels


def nominal_dots(length: F) -> tuple[F, int]:
    for dots, factor in ((0, F(1)), (1, F(3, 2)), (2, F(7, 4))):
        if length / factor in (F(4), F(2), F(1), F(1, 2), F(1, 4), F(1, 8)):
            return length, dots
    raise ValueError(f"no note value for {length}")


class Decoder:
    def __init__(self, path: str, key_hint: int | None = None) -> None:
        self.doc = pymupdf.open(path)
        self.music = font_map(self.doc)
        self.bars: list[Bar] = []
        self.corrections: list[Issue] = []
        self.flags: list[Issue] = []
        self.time = (4, 4)
        self.time_len = F(4)
        self.key = 0
        self.second = False
        self.first = True
        self.prev_chord: Chord | None = None
        self.open_ties: list[Head] = []
        self.printed: list[tuple[int, int]] = []

    def run(self) -> Score:
        for page_no, page in enumerate(self.doc):
            self.page(page_no, page)
        self.finish_voltas()
        for ours, theirs in self.printed:
            if ours != theirs:
                self.flags.append(Issue(ours, f"printed bar number {theirs}"))
        return Score(self.bars, self.corrections, self.flags, self.second)

    def finish_voltas(self) -> None:
        for i, bar in enumerate(self.bars):
            if bar.volta:
                j = i
                if bar.volta == "1":
                    while j + 1 < len(self.bars) and not self.bars[j].repeat_end:
                        j += 1
                self.bars[j].volta_stop = bar.volta

    def page(self, page_no: int, page: pymupdf.Page) -> None:
        glyphs, spans = load(page, self.music)
        self.threes = [(g.x, g.y) for g in glyphs if g.code == 0x33 and g.size < 17]
        self.threes += [(s.x, s.y) for s in spans if s.text.strip() == "3" and s.size > 8.5]
        spans = [s for s in spans if not (s.text.strip() == "3" and s.size > 8.5)]
        bases = sorted({round(g.y, 1) for g in glyphs if g.code == STAFF})
        merged: list[float] = []
        for b in bases:
            if not merged or b - merged[-1] > 3:
                merged.append(b)
        bases = merged
        size = next(g.size for g in glyphs if g.code == STAFF)
        def owner(y: float, x: float = 0) -> float:
            return min(bases, key=lambda b: abs(y - (b - 8.6)))
        staff_glyphs: dict[float, list[G]] = {b: [] for b in bases}
        for g in glyphs:
            if abs(g.size - size) < 0.5 or g.code == GRACE:
                staff_glyphs[owner(g.y)].append(g)
        drawings = page.get_drawings()
        beams: list[Any] = []
        curves = []
        lines = []
        boxes = []
        for d in drawings:
            r = d["rect"]
            kinds = [i[0] for i in d["items"]]
            if d.get("fill") is not None and "c" not in kinds and r.width > 3 and 1.0 <= r.height < 16:
                beams.extend(pymupdf.Rect(i[1]) for i in d["items"]) if set(kinds) == {"re"} else beams.append(r)
            elif kinds == ["c"]:
                curves.append(r)
            elif kinds == ["l"]:
                it = d["items"][0]
                lines.append((it[1].x, it[1].y, it[2].x, it[2].y))
            elif kinds == ["re"]:
                boxes.append(r)
        # 2-staff systems: consecutive staves close together sharing bar lines
        systems: list[list[float]] = []
        i = 0
        while i < len(bases):
            b = bases[i]
            if i + 1 < len(bases) and bases[i + 1] - b < 120 and self.same_bars(staff_glyphs[b], staff_glyphs[bases[i + 1]]):
                systems.append([b, bases[i + 1]])
                i += 2
            else:
                systems.append([b])
                i += 1
        first_on_page = True
        for s_i, sys in enumerate(systems):
            self.system(page_no, sys, staff_glyphs, spans, glyphs, beams, curves, lines, boxes, size, first_on_page, systems, s_i)
            first_on_page = False

    @staticmethod
    def bar_xs(gl: list[G]) -> list[float]:
        return sorted({round(g.x, 1) for g in gl if g.code in BARS})

    def same_bars(self, a: list[G], b: list[G]) -> bool:
        xa, xb = self.bar_xs(a), self.bar_xs(b)
        return len(xa) >= 2 and len(xa) == len(xb) and all(abs(p - q) < 2.5 for p, q in zip(xa, xb, strict=False))

    def system(self, page_no: int, sys: list[float], staff_glyphs: dict[float, list[G]], spans: list[Span], glyphs: list[G], beams: list[Any], curves: list[Any], lines: list[Any], boxes: list[Any], size: float, first_on_page: bool, systems: list[list[float]], s_i: int) -> None:
        top = sys[0]
        raw = sorted((round(g.x, 1), g.code) for g in staff_glyphs[top] if g.code in BARS)
        col_list: list[tuple[float, float, set[int]]] = []  # left x, right x, codes of 1 printed bar line
        for x, code in raw:
            if col_list and x - col_list[-1][1] < 7:
                col_list[-1] = (col_list[-1][0], x, col_list[-1][2] | {code})
            else:
                col_list.append((x, x, {code}))
        def heard(lo: float, hi: float) -> bool:
            return any(lo <= g.x < hi and (g.code in HEADS or g.code in RESTS or g.code in (BAR_REST, SIMILE)) for b in sys for g in staff_glyphs[b])
        clefs = [g.x for g in staff_glyphs[top] if g.code == CLEF]
        if clefs and col_list and min(clefs) < col_list[0][0] - OPENING_BAR_MIN and heard(min(clefs), col_list[0][0]):
            col_list.insert(0, (min(clefs), min(clefs), set()))  # an opening line printed without its left bar line
        if len(col_list) > 1 and not heard(col_list[0][1], col_list[1][0]):
            col_list = col_list[1:] if len(col_list[1][2]) > 1 or col_list[1][0] - col_list[0][1] < OPENING_BAR_MIN else col_list
        {c[0]: c[2] for c in col_list}
        pairs = [(a, b) for a, b in zip(col_list, col_list[1:], strict=False)]
        # column region of this system
        prev_base = systems[s_i - 1][-1] if s_i else None
        next_base = systems[s_i + 1][0] if s_i + 1 < len(systems) else None
        y_lo = 0 if prev_base is None else (prev_base + top) / 2 + 8
        y_hi = sys[-1] + 54 if next_base is None else (sys[-1] + next_base) / 2 + 8
        chord_lo = 0 if prev_base is None else prev_base + 2
        chord_hi = sys[-1] - 4
        def mine(s: Span) -> bool:
            if self.is_chord(s):
                return chord_lo <= s.y < chord_hi
            if re.fullmatch(r"[12]\.", s.text.strip()) or (s.size < 9.5 and s.bold) or any(abs(s.y - o.y) < 3 and o.text.strip().startswith("(After") for o in spans) or (s.bold and REHEARSAL.fullmatch(s.text.strip())):
                return chord_lo <= s.y < top  # marks stand above the staff they label
            return y_lo <= s.y < y_hi
        my_spans = [s for s in spans if mine(s)]
        new_bars: list[Bar] = []
        for p_i, (col_a, col_b) in enumerate(pairs):
            left, right = col_a[1], col_b[0]
            bar = Bar(len(self.bars) + 1, page_no, left, right, self.time_len)
            bar.base = top
            bar.new_system = p_i == 0 and bool(self.bars)
            bar.new_page = p_i == 0 and first_on_page and bool(self.bars)
            if 0xF07B in col_a[2] and 0xF0D3 not in col_a[2]:
                bar.repeat_start = True
            if 0xF07B in col_b[2] and 0xF0D3 in col_b[2]:
                bar.repeat_end = True
            elif 0xF07B not in col_b[2] and 0xF0D3 in col_b[2]:
                bar.final = True
            elif 0xF0D2 in col_b[2] and 0xF07B not in col_b[2]:
                bar.double = True
            self.bar_contents(bar, sys, staff_glyphs, beams, size)
            self.bars.append(bar)
            new_bars.append(bar)
        self.keysig(new_bars, staff_glyphs[top])
        self.curves(new_bars, sys, curves)
        self.apply_pitch(new_bars)
        self.chords_and_text(new_bars, sys, my_spans, glyphs, size, s_i)
        self.signs(new_bars, staff_glyphs[top], glyphs, size, top, chord_lo, top + 5)
        self.wedges(new_bars, lines, top, y_hi)
        self.volta(new_bars, my_spans)

    def keysig(self, bars: list[Bar], top: list[G]) -> None:
        if not bars or self.first is False:
            return
        first = bars[0]
        flats = sum(g.code in FLATS and first.left < g.x < first.left + 60 for g in top)
        sharps = sum(g.code in SHARPS and first.left < g.x < first.left + 60 for g in top)
        self.key = sharps - flats
        first.key = self.key
        self.first = False

    def bar_contents(self, bar: Bar, sys: list[float], staff_glyphs: dict[float, list[G]], beams: list[Any], size: float) -> None:
        self.top_base = sys[0]
        for idx, base in enumerate(sys):
            self.current_base = base
            near = [g for g in staff_glyphs[base] if bar.left <= g.x < bar.right - 0.5]
            if idx == 0:
                self.time_change(bar, near, base)
            voices = self.read_staff(bar, base, near, staff_glyphs[base], beams, size)
            if idx == 0:
                bar.voices = voices
            else:
                bar.voices2 = voices
                self.second = True

    def time_change(self, bar: Bar, near: list[G], base: float) -> None:
        digits = sorted((g for g in near if 0x31 <= g.code <= 0x39 and abs(g.size - 17.9) < 1), key=lambda g: g.y)
        sym = [g for g in near if g.code == 0xF043]
        new = None
        if sym:
            new = (4, 4)
            bar.time_symbol = "cut"
        elif len(digits) >= 2:
            new = (digits[0].code - 0x30, digits[-1].code - 0x30)
        if new:
            bar.time = new
            self.time = new
            self.time_len = F(new[0] * 4, new[1])
            bar.length = self.time_len

    def read_staff(self, bar: Bar, base: float, near: list[G], whole_staff: list[G], beams: list[Any], size: float) -> list[list[Event]]:
        step = STEP_PER_SIZE * size
        stems = [g for g in whole_staff if g.code in (0xF0C7, 0xF0C8)]
        events: list[Event] = []
        bar_rest = False
        stem_xs = []
        for h in near:
            if h.code in HEADS:
                got = [s for s in stems if abs(s.x - h.x) < 1.2 and abs(s.y - h.y) < 45]
                if got:
                    stem_xs.append(h.x + (0.02 if got[0].code == 0xF0C8 else 0.31) * size)
        for g in near:
            if g.code in HEADS:
                kind, stem = HEADS[g.code]
                own = [s for s in stems if abs(s.x - g.x) < 1.2 and abs(s.y - g.y) < 45]
                if stem == "up" and kind in ("black", "half"):
                    own = [s for s in own if s.code == 0xF0C7 and s.y <= g.y + 0.6] or own
                if stem == "down" and kind in ("black", "half"):
                    own = [s for s in own if s.code == 0xF0C8 and s.y >= g.y - 0.6] or own
                if stem is None or kind == "whole":
                    stem = None
                nominal = {"black": F(1), "x": F(1), "circlex": F(1), "diamond": F(1), "filleddiamond": F(1), "half": F(2), "whole": F(4)}[kind]
                stem_x = g.x + 0.31 * size if stem == "up" else g.x + 0.02 * size
                if kind == "half" and not own:
                    nominal = bar.length
                    stem = None
                if own:
                    stem = "down" if own[0].code == 0xF0C8 else "up"
                    stem_x = g.x + 0.31 * size if stem == "up" else g.x + 0.02 * size
                level = 0
                beam_id = None
                if stem:
                    tips = [s.y for s in own]
                    tip = (min(tips, default=g.y) - 4.3) if stem == "up" else (max(tips, default=g.y) + 4.3)
                    lo, hi = sorted((g.y, tip))
                    hits = [b for b in beams if lo - 8 <= b.y0 and b.y1 <= hi + 8 and (
                        b.x0 - 1 <= stem_x <= b.x1 + 1
                        or (b.x1 <= stem_x <= b.x1 + 2.2 and not any(abs(o - b.x0) < 1.5 for o in stem_xs)))]
                    level = beam_levels([(b.y0 + b.y1) / 2 for b in hits])
                    if hits:
                        beam_id = id(max(hits, key=lambda b: b.width))  # the primary beam spans its whole group
                    for fl in near:
                        if fl.code in FLAGS and abs(fl.x - g.x) < 1.2:
                            level = max(level, FLAGS[fl.code])
                    if level and kind in ("black", "x", "circlex", "diamond", "filleddiamond"):
                        nominal = nominal / (2**level)
                dots = int(any(d.code == DOT and 2 < d.x - g.x < 12 and abs(d.y - g.y) < 3.2 for d in near))
                if dots:
                    nominal = nominal * F(3, 2)
                accidental = None
                for a in near:
                    if 0 < g.x - a.x < 11 and abs(a.y - g.y) < 1.4:
                        if a.code in FLATS:
                            accidental = "b"
                        elif a.code in SHARPS:
                            accidental = "#"
                        elif a.code in NATURALS:
                            accidental = "n"
                steps = round((base - 0.2 - g.y) / step)
                index = 2 + steps + 4 * 7
                shape = kind if kind in ("x", "circlex", "diamond", "filleddiamond") else "normal"
                head = Head(g.x, g.y, LETTERS[index % 7], index // 7, accidental, shape)
                ev = Event(g.x, [head], nominal, dots, stem, beam=beam_id)
                ev.accent = any(a.code == 0xF03E and abs(a.x - g.x) < 7 for a in near)
                ev.trill = any(t.code == 0xF0D9 and abs(t.x - g.x) < 7 for t in near)
                ev.fermata = any(t.code == 0xF055 and abs(t.x - g.x) < 7 for t in near)
                events.append(ev)
            elif g.code == GRACE:
                index = 2 + round((base - 0.2 - g.y) / step) + 4 * 7
                events.append(Event(g.x, [Head(g.x, g.y, LETTERS[index % 7], index // 7)], F(1, 2), 0, "up", grace=True))
            elif g.code in RESTS:
                length = RESTS[g.code]
                dots = int(any(d.code == DOT and 1 < d.x - g.x < 12 and abs(d.y - g.y) < 6 for d in near))
                if dots:
                    length = length * F(3, 2)
                events.append(Event(g.x, [], length, dots))
            elif g.code == BAR_REST:
                bar_rest = True
            elif g.code == SIMILE:
                t = Text(g.x, g.y, [("//", True, False)], True, size=10)
                (bar.texts if base == self.top_base else bar.texts2).append(t)
        events.sort(key=lambda e: e.x)
        return self.voices(bar, events, bar_rest, near)

    def voices(self, bar: Bar, events: list[Event], bar_rest: bool, near: list[G]) -> list[list[Event]]:
        # tuplets marked by a 3
        chords: list[Event] = []
        for ev in events:
            for other in chords:
                if abs(other.x - ev.x) < 1.3 and ev.heads and other.heads and other.nominal == ev.nominal and other.stem == ev.stem:
                    other.heads += ev.heads
                    break
            else:
                chords.append(ev)
        for ev in chords:
            ev.heads = list({(h.step, h.octave): h for h in ev.heads}.values())  # a source can stamp 1 head twice
            ev.heads.sort(key=lambda h: -h.y)
        for tx, ty in self.threes:
            if bar.left <= tx < bar.right and chords and abs(ty - self.current_base) < 45:
                for ev in sorted(chords, key=lambda e: abs(e.x - tx))[:3]:
                    ev.tuplet = True
        if not chords:
            return [[Event(bar.left, [], bar.length, bar_rest=True)]]
        rests_only = all(not e.heads for e in chords)
        total = sum((e.length for e in chords), F(0))
        if total == bar.length:
            return [self.timed(chords)]
        self.mark_short(bar, chords)
        groups: dict[int, list[Event]] = {}
        for ev in chords:
            if ev.beam is not None:
                groups.setdefault(ev.beam, []).append(ev)
        triple = [g for g in groups.values() if len(g) == 3 and len({e.nominal for e in g}) == 1]
        if triple and sum((e.length for e in chords), F(0)) - sum((sum((e.length for e in g), F(0)) / 3 for g in triple), F(0)) == bar.length:
            for g in triple:  # the printed 3 is a vector path rather than a glyph
                for ev in g:
                    ev.tuplet = True
        total = sum((e.length for e in chords), F(0))
        if total == bar.length:
            return [self.timed(chords)]
        # 2 rhythms on 1 staff: split by stem direction
        up = [e for e in chords if e.stem == "up"]
        down = [e for e in chords if e.stem == "down"]
        free = [e for e in chords if e.stem is None]
        for layout in ((up + free, down), (up, down + free), (free, up + down)):
            a = sorted(layout[0], key=lambda e: e.x)
            b = sorted(layout[1], key=lambda e: e.x)
            if a and b:
                s_a, s_b = sum((e.length for e in a), F(0)), sum((e.length for e in b), F(0))
                if (s_a == bar.length and s_b <= bar.length) or (s_b == bar.length and s_a <= bar.length):
                    return self.two_voices(bar, a, b)
        if rests_only and all(e.nominal == chords[0].nominal for e in chords) and len(chords) > 1:
            each = bar.length / len(chords)
            self.corrections.append(Issue(bar.number, f"{len(chords)} rests of {chords[0].nominal} quarters cannot fill the {bar.length}-quarter bar; written as {each} each"))
            for ev in chords:
                ev.nominal, ev.dots = nominal_dots(each)
            return [self.timed(chords)]
        if total < bar.length and not chords[-1].heads:
            new = chords[-1].length + bar.length - total
            self.corrections.append(Issue(bar.number, f"bar printed {total} quarters of {bar.length}; closing rest {chords[-1].length} written as {new}"))
            chords[-1].nominal, chords[-1].dots = nominal_dots(new)
            return [self.timed(chords)]
        repaired = self.repair(bar, chords, total)
        if repaired:
            return repaired
        self.flags.append(Issue(bar.number, f"bar reads {total} quarters against {bar.length}: " + " ".join(self.names(chords))))
        return [self.pad(bar, chords)]

    def repair(self, bar: Bar, chords: list[Event], total: F) -> list[list[Event]] | None:
        """Bars the printed values cannot fill; the bar length is the evidence, and each repair but a voice split is listed."""
        by_stem = stem_split(chords, bar.length)
        if by_stem:
            return self.two_voices(bar, *by_stem)
        close = [(a, b) for i, a in enumerate(chords) for b in chords[i + 1:] if abs(a.x - b.x) < COINCIDENT_X]
        if close and len(chords) <= MAX_SPLIT_EVENTS:
            split = voice_split(chords, close, bar.length)
            if split:
                if all(not e.heads for e in split[1]):
                    return [self.timed(split[0])]  # a stray rest over a note that already fills the bar
                return self.two_voices(bar, *split)
        if all(e.nominal == F(1, 2) for e in chords) and len(chords) * F(1, 3) == bar.length:
            for ev in chords:
                ev.tuplet = True
            return [self.timed(chords)]
        shortest = min((e.length for e in chords if e.heads), default=F(0))
        tiny = [e for e in chords if e.heads and e.length == shortest]
        if total < bar.length and len(tiny) > 1 and sum((e.length for e in tiny), F(0)) == bar.length - total:
            self.corrections.append(Issue(bar.number, f"bar printed {total} quarters of {bar.length}; notes of {shortest} written as {shortest * 2}"))
            for ev in tiny:
                ev.nominal = ev.nominal * 2
            return [self.timed(chords)]
        last = chords[-1]
        new = last.length + bar.length - total
        if last.heads and new in REPAIR_LENGTHS and not last.tuplet:
            self.corrections.append(Issue(bar.number, f"bar printed {total} quarters of {bar.length}; last note {last.length} written as {new}"))
            last.nominal, last.dots = nominal_dots(new)
            return [self.timed(chords)]
        return None

    def mark_short(self, bar: Bar, chords: list[Event]) -> None:
        """A circled x carries no length of its own; it fills what the bar leaves."""
        unknown = [e for e in chords if e.heads and e.heads[0].shape == "circlex"]
        total = sum((e.length for e in chords), F(0))
        if len(unknown) == 1 and total < bar.length:
            ev = unknown[0]
            new = ev.length + bar.length - total
            if new in (F(1), F(2), F(4), F(3, 2), F(3), F(1, 2)):
                ev.dots = 1 if new in (F(3, 2), F(3)) else 0
                ev.nominal = new

    @staticmethod
    def names(chords: list[Event]) -> list[str]:
        return [("+".join(h.step + str(h.octave) for h in e.heads) or "rest") + ":" + str(e.length) for e in chords]

    def timed(self, voice: list[Event]) -> list[Event]:
        t = F(0)
        for ev in voice:
            ev.onset = t
            t += ev.length
        return voice

    def pad(self, bar: Bar, chords: list[Event]) -> list[Event]:
        voice = self.timed(chords)
        total = sum((e.length for e in voice), F(0))
        if total < bar.length:
            voice += self.fill(bar.length - total, chords[-1].x + 1, total)
        return voice

    def fill(self, gap: F, x: float, onset: F) -> list[Event]:
        out = []
        for size in (F(4), F(3), F(2), F(3, 2), F(1), F(3, 4), F(1, 2), F(1, 4), F(1, 8)):
            while gap >= size:
                dots = 1 if size in (F(3), F(3, 2), F(3, 4)) else 0
                out.append(Event(x, [], size, dots, hidden=True, onset=onset))
                onset += size
                gap -= size
        return out

    def two_voices(self, bar: Bar, a: list[Event], b: list[Event]) -> list[list[Event]]:
        s_a = sum((e.length for e in a), F(0))
        if s_a == bar.length and sum((e.length for e in b), F(0)) == bar.length:
            return [self.timed(a), self.timed(b)]
        full, other = (a, b) if s_a == bar.length else (b, a)
        self.timed(full)
        anchors = [(e.x, e.onset) for e in full]
        out = []
        t = F(0)
        for ev in other:
            onset = min(anchors, key=lambda p: abs(p[0] - ev.x))[1]
            if onset > t:
                out += self.fill(onset - t, ev.x - 1, t)
                t = onset
            ev.onset = t
            out.append(ev)
            t += ev.length
        if t < bar.length:
            out += self.fill(bar.length - t, other[-1].x + 1, t)
        return [a if a is full else out, out if a is full else b] if False else ([full, out] if a is full else [out, full])

    def curves(self, bars: list[Bar], sys: list[float], curves: list[Any]) -> None:
        seen: list[tuple[float, float, float]] = []
        all_ev = [(ev, bar) for bar in bars for voices in (bar.voices, bar.voices2) for voice in voices for ev in voice if ev.heads]
        unique = []
        for r in curves:
            key = (r.x0, r.x1, r.y0)
            if any(abs(k[0] - key[0]) < 1 and abs(k[1] - key[1]) < 1 and abs(k[2] - key[2]) < 2.5 for k in seen):
                continue
            seen.append(key)
            unique.append(r)
        for r in unique:
            mid = (r.y0 + r.y1) / 2
            if not any(s - 40 < mid < s + 24 for s in sys):
                continue
            left = [e for e, _ in all_ev if -9 <= e.x - r.x0 <= 2 and any(abs(h.y - mid) < 16 for h in e.heads)]
            right = [e for e, _ in all_ev if -7 <= e.x - r.x1 <= 9 and e.x > r.x0 + 2 and any(abs(h.y - mid) < 16 for h in e.heads)]
            if not left:
                continue
            l_ev = min(left, key=lambda e: abs(e.x - r.x0))
            if not right:
                l_ev.heads[0].tie_start = True  # continues on the next system
                self.open_ties.append(l_ev.heads[0])
                continue
            r_ev = min(right, key=lambda e: abs(e.x - r.x1))
            pairs = [(a, b) for a in l_ev.heads for b in r_ev.heads if a.step == b.step and a.octave == b.octave]
            if pairs:
                pairs[0][0].tie_start = True
                pairs[0][1].tie_stop = True
            else:
                l_ev.heads[0].slur_start = True
                r_ev.heads[0].slur_stop = True

    def apply_pitch(self, bars: list[Bar]) -> None:
        order = "FCGDAEB"
        for bar in bars:
            for voices in (bar.voices, bar.voices2):
                state: dict[tuple[str, int], int] = {}
                for voice in voices:
                    for ev in voice:
                        for h in ev.heads:
                            key = (h.step, h.octave)
                            if h.accidental:
                                state[key] = {"b": -1, "#": 1, "n": 0}[h.accidental]
                            default = 0
                            if self.key > 0 and order.index(h.step) < self.key:
                                default = 1
                            if self.key < 0 and order[::-1].index(h.step) < -self.key:
                                default = -1
                            h.alter = state.get(key, default)
        # a tie carries the alteration across the bar line
        flat = [ev for bar in self.bars for voice in bar.voices[:1] for ev in voice if ev.heads]
        for a, b in zip(flat, flat[1:], strict=False):
            for h in a.heads:
                if h.tie_start:
                    for q in b.heads:
                        if q.step == h.step and q.octave == h.octave:
                            q.tie_stop = True
                            q.alter = h.alter

    def chords_and_text(self, bars: list[Bar], sys: list[float], spans: list[Span], glyphs: list[G], size: float, s_i: int) -> None:
        clef_x = min((g.x + 1 for g in glyphs if g.code == CLEF and abs(g.y - sys[0]) < 8), default=0.0)
        small_flats = [g for g in glyphs if g.code in FLATS | SHARPS and abs(g.size - size) > 0.5 and g.size > 10]
        items = []
        for s in spans:
            if any(n in s.text for n in NOISE) or s.size > 20:
                continue
            if bars and bars[0].number == 1 and s.y < sys[0] - CREDIT_CLEARANCE and (s.x > CREDIT_X or s.y < MARK_Y):
                continue  # credits sit in the top corners, well above the first staff's chords and cues
            if abs(s.size - 8.0) < 0.3 and s.text.strip().isdigit() and (not s.font.startswith("Times") or s.x < clef_x) and bars and s.y > sys[0] - 45 and s.x < bars[0].left + 40:
                self.printed.append((bars[0].number, int(s.text)))
                continue  # printed bar number
            items.append(s)
        used = set()
        for s in items:
            if not self.is_chord(s):
                continue
            used.add(id(s))
        # merge rows: superscripts ride a little higher
        allitems = sorted((s for s in items if id(s) in used), key=lambda s: (s.size < 9, s.y))
        groups: list[list[Span]] = []
        for s in allitems:
            for g in groups:
                if abs(g[0].y - s.y) < 6.5:
                    g.append(s)
                    break
            else:
                groups.append([s])
        for g in groups:
            ys = g[0].y
            pieces = [(s.x, s.x1, s.text.strip()) for s in g]
            for fl in small_flats:
                if abs(fl.y - ys + 5) < 8:
                    pieces.append((fl.x, fl.x + 3, "b" if fl.code in FLATS else "#"))
            for x, text in assemble_chords(pieces):
                chord = parse_chord(self.fix_accidentals(text), self.prev_chord, x)
                if chord is None:
                    self.flags.append(Issue(bars[0].number if bars else 0, f"unread chord {text!r}"))
                    continue
                bar = next((b for b in bars if x < b.right - 11), bars[-1] if bars else None)  # a label centred on a first note starts left of the bar line
                if bar is None:
                    continue
                bar.chords.append(chord)
                self.prev_chord = chord
        self.place_texts(bars, sys, [s for s in items if id(s) not in used], s_i)

    @staticmethod
    def fix_accidentals(text: str) -> str:
        return text

    def is_chord(self, s: Span) -> bool:
        text = s.text.strip()
        if s.bold or s.italic:
            return False
        if abs(s.size - 8.0) < 0.3:
            return bool(re.fullmatch(r"[0-9()#badd♭♯susmajdim+/]+", text))
        if abs(s.size - 10.8) < 0.3:
            return bool(re.fullmatch(r"(/?[A-G](b|#)?(maj|m|dim|sus|add|aug)?[0-9()#b]*(/[A-G]?)?|maj|m|sus|dim)", text)) and self.chord_font(s)
        return False

    def chord_font(self, s: Span) -> bool:
        return "Verdana" not in s.font and not s.font.endswith("FPEF") or s.font.startswith("Times")

    def place_texts(self, bars: list[Bar], sys: list[float], spans: list[Span], s_i: int) -> None:
        if not bars:
            return
        groups: list[list[Span]] = []
        for s in sorted(spans, key=lambda s: s.x):
            for g in groups:
                if abs(g[-1].y - s.y) < 2.5 and -2 < s.x - g[-1].x1 < 7:
                    g.append(s)
                    break
            else:
                groups.append([s])
        for group in sorted(groups, key=lambda g: g[0].y):
            first = group[0]
            text = "".join(s.text for s in group).strip()
            if not text:
                continue
            if first.bold and first.y < sys[0] - 10 and "(" not in text and (first.size < 9.5 and len(text) <= 10 or REHEARSAL.fullmatch(text)):
                bar = next((b for b in bars if b.left - 15 <= first.x < b.right), bars[0])
                bar.rehearsal.append(text)
                continue
            if re.fullmatch(r"[12]\.", text):
                continue
            if first.size > 12 and first.bold and first.italic and s_i == 0:
                bars[0].tempo = (re.sub(r"\s*=.*$", "", text), self.tempo_value(spans))
                continue
            if re.fullmatch(r"=?\s*\d+", text.replace(" ", "")) or text.startswith("="):
                continue
            base = min(sys, key=lambda b: abs(first.y - (b - 8.6)))
            bar = next((b for b in bars if first.x < b.right - 2), bars[-1])
            runs: list[tuple[str, bool, bool]] = []
            for i, s in enumerate(group):
                piece = s.text.strip() if len(group) == 1 else s.text
                if i and s.x - group[i - 1].x1 > 1.5 and not piece.startswith(" ") and not runs[-1][0].endswith(" "):
                    piece = " " + piece
                runs.append((piece, s.bold, s.italic))
            label_at = next((i for i, run in enumerate(runs) if i and ":" in run[0] and run[1] and run[2]), 0)
            if label_at and not any(":" in run[0] for run in runs[:label_at]):
                # a source can print a cue on top of a lyric line; give the cue its own line
                head = runs[label_at:]
                runs = [(head[0][0].strip(), head[0][1], head[0][2]), *head[1:], ("\n" + runs[0][0], runs[0][1], runs[0][2]), *runs[1:label_at]]
            t = Text(first.x, first.y, runs, first.y < base - 8, size=first.size * 0.9)
            target = bar.texts if base == sys[0] else bar.texts2
            lyric = [o for o in target if o.runs[0][1] and o.runs[0][2] and ":" in o.runs[0][0]]
            if lyric and first.bold and first.italic and ":" in runs[0][0] and abs(lyric[0].y - first.y) < 20:
                lyric[0].runs += [("\n" + runs[0][0], runs[0][1], runs[0][2])] + runs[1:]
            else:
                cue = [o for o in target if first.bold and first.italic and ":" in runs[0][0] and o.runs[0][1] and o.runs[0][2] and abs(o.y - first.y) < 20]
                for o in cue:  # a cue printed over the lyric block gets its own line under it
                    target.remove(o)
                    t.runs += [("\n" + o.runs[0][0], o.runs[0][1], o.runs[0][2]), *o.runs[1:]]
                target.append(t)

    @staticmethod
    def tempo_value(spans: list[Span]) -> float:
        values = [float(m[1].replace(" ", "")) for s in spans if (m := re.search(r"=\s*(\d+(?:\s*\.\s*\d+)?)", s.text))]
        if not values:
            raise ValueError("no tempo printed")
        return min(values)  # a sheet may also print the eighth-note equivalent, twice the quarter value

    def signs(self, bars: list[Bar], top: list[G], glyphs: list[G], size: float, base: float, y_lo: float, y_hi: float) -> None:
        if not bars:
            return
        span = bars[-1].right - bars[0].left
        for g in glyphs:
            if not y_lo <= g.y < y_hi:
                continue
            if g.code in (0xF025, 0xF0DE):
                left = g.x < bars[0].left + span * 0.4
                if g.code == 0xF025:
                    if left:
                        bars[0].segno = True
                    else:
                        bars[-1].dal_segno = True
                else:
                    if left:
                        bars[0].coda = True
                    else:
                        bars[-1].to_coda = True

    def wedges(self, bars: list[Bar], lines: list[Any], top: float, y_hi: float) -> None:
        if not bars:
            return
        cand = [seg for seg in lines if top - 40 < seg[1] < y_hi and abs(seg[2] - seg[0]) > 10]
        for i, a in enumerate(cand):
            for b in cand[i + 1:]:
                if abs(a[2] - b[0]) < 0.5 and abs(a[3] - b[1]) < 0.5:
                    far_gap = abs(a[1] - b[3])
                    if abs(a[0] - b[2]) > 1.5 or not 1 < far_gap < 14:
                        continue
                    apex = a[2]
                    far = a[0]
                    kind = "crescendo" if apex < far else "diminuendo"
                    x0, x1 = min(apex, far), max(apex, far)
                    start = next((n for n in bars if n.left <= x0 < n.right), bars[0])
                    stop = next((n for n in bars if n.left < x1 <= n.right + 3), bars[-1])
                    start.wedges.append((x0, x1 if stop is start else -1, kind))
                    if stop is not start:
                        stop.wedges.append((-1, x1, kind))

    def volta(self, bars: list[Bar], spans: list[Span]) -> None:
        for s in spans:
            text = s.text.strip()
            if re.fullmatch(r"[12]\.", text) and s.bold:
                bar = next((b for b in bars if b.left - 4 <= s.x < b.right), None)
                if bar:
                    bar.volta = text[0]


def decode(path: str) -> Score:
    return Decoder(path).run()
