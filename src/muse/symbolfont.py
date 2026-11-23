"""Decode the symbol-font symbol encoding from a vector PDF into the score model."""

from __future__ import annotations

import dataclasses
import itertools
import re
from fractions import Fraction
from typing import Any

import pymupdf

from muse.model import LETTERS, Bar, Chord, Event, Head, Issue, Score, Text, UnknownGlyph, bar_at

HEADS = {
    0xF0CF: ("black", "down"),
    0xF0F6: ("black", "up"),
    0xF0F3: ("black", "up"),
    0xF0F5: ("half", "down"),
    0xF0FA: ("half", "up"),
    0xF077: ("whole", None),
    0xF0C0: ("x", "up"),
    0xF0E1: ("diamond", "up"),
    0xF0EB: ("circlex", "up"),
    0xF0E2: ("filleddiamond", "down"),
}
GRACE = 0xF0F7
DOUBLE_TOP = 0x6D
TEMPO_EIGHTH = 0x65
RESTS = {0xF0E4: Fraction(1, 2), 0xF0C5: Fraction(1, 4), 0xF0CE: Fraction(1), 0xF0EE: Fraction(2)}
CLEF = 0xF026
BAR_REST, SIMILE, STAFF, DOT = 0xF0B7, 0xF022, 0xF03D, 0xF02E
BARS = {0xF06C, 0xF07B, 0xF0D2, 0xF0D3}
FLAGS = {0xF06A: 1, 0xF06B: 2, 0xF04A: 1, 0xF04B: 2}
DYNAMIC_LETTERS = {0x66: "f", 0x6D: "m", 0x70: "p", 0x73: "s", 0x7A: "z"}
TEXT_NATURAL = 0xA7
TENUTO, MARCATO, MARCATO_BELOW, ACCENT_BELOW = 0xF02D, 0xF05E, 0xF076, 0xF027
UP_BOW, DOWN_BOW, HARMONIC = 0xF0B2, 0xF0B3, 0xF06F
BASS_CLEF, ALTO_CLEF, TIME_FOUR = 0xF03F, 0xF042, 0xF034
AUGMENTATION_DOT, BRACKET_TOP, BRACKET_BOTTOM, ARPEGGIO = 0xF0AA, 0xF0A1, 0xF0A2, 0xF0B8
OCTAVE_BELOW, DIAMOND_WHOLE = 0xF0DC, 0xF07E
ORCHESTRAL_GLYPHS = {
    0x66: "dynamic letter f",
    0x70: "dynamic letter p",
    0x73: "dynamic letter s",
    0x7A: "dynamic letter z",
    TEXT_NATURAL: "natural inside text",
    TENUTO: "tenuto",
    MARCATO: "marcato",
    MARCATO_BELOW: "marcato below",
    ACCENT_BELOW: "accent below",
    UP_BOW: "up-bow",
    DOWN_BOW: "down-bow",
    HARMONIC: "harmonic circle",
    BASS_CLEF: "bass clef",
    ALTO_CLEF: "alto clef",
    TIME_FOUR: "time digit 4",
    AUGMENTATION_DOT: "augmentation dot",
    BRACKET_TOP: "system bracket top end",
    BRACKET_BOTTOM: "system bracket bottom end",
    ARPEGGIO: "wavy arpeggio line",
    OCTAVE_BELOW: "treble-8vb clef's 8",
    DIAMOND_WHOLE: "diamond notehead",
}
KNOWN_GLYPHS = (
    frozenset((
        0x20,
        0x23,
        TEMPO_EIGHTH,
        DOUBLE_TOP,
        0xF0E2,
        0xF0F7,
        0x2E,
        0x31,
        0x32,
        0x33,
        0x34,
        0x35,
        0x36,
        0x37,
        0x38,
        0x39,
        0x61,
        0x62,
        0x69,
        0x71,
        0x74,
        0x75,
        0x76,
        0xA3,
        0xF022,
        0xF023,
        0xF025,
        0xF026,
        0xF02E,
        0xF03D,
        0xF03E,
        0xF043,
        0xF04A,
        0xF04B,
        0xF055,
        0xF05F,
        0xF062,
        0xF06A,
        0xF06B,
        0xF06C,
        0xF06E,
        0xF077,
        0xF07B,
        0xF0B7,
        0xF0C0,
        0xF0C5,
        0xF0C7,
        0xF0C8,
        0xF0CE,
        0xF0CF,
        0xF0D2,
        0xF0D3,
        0xF0D9,
        0xF0DE,
        0xF0E1,
        0xF0E4,
        0xF0EB,
        0xF0EE,
        0xF0F3,
        0xF0F5,
        0xF0F6,
        0xF0FA,
    ))
    | ORCHESTRAL_GLYPHS.keys()
)
LEAD_SHEET_GLYPHS = KNOWN_GLYPHS - ORCHESTRAL_GLYPHS.keys()
FLATS, SHARPS, NATURALS = {0x62, 0xF062}, {0x23, 0xF023}, {0x6E, 0xF06E}
STEP_PER_SIZE = 2.16 / 17.9
CREDIT_ROW = 2.0
COINCIDENT_X = 2.0
MAX_SPLIT_RESTS = 7
BEAM_GAP = 0.9
MAX_SPLIT_EVENTS = 14
REPAIR_LENGTHS = (
    Fraction(1, 4),
    Fraction(1),
    Fraction(3, 2),
    Fraction(2),
    Fraction(3),
    Fraction(4),
    Fraction(3, 4),
    Fraction(1, 2),
)
OPENING_BAR_MIN = 45.0
MARK_Y = 40.0
NOISE = ("Sáng tác", "Trình bày", "Lời Việt", "Singer", "Copyright", "Contact")


PUA_START = 0xF000
MUSIC_FONT_MIN_PUA = 20
CHORD_JOIN_GAP = 3
REST_DROPPED = 2
DIGIT_ONE, DIGIT_THREE, DIGIT_NINE = 0x31, 0x33, 0x39
TUPLET_GLYPH_MAX_SIZE = 17
TUPLET_TEXT_MIN_SIZE = 8.5
STAFF_MERGE_GAP = 3
STAFF_SIZE_TOLERANCE = 0.5
BEAM_MIN_WIDTH = 3
BEAM_MAX_HEIGHT = 16
GRAND_STAFF_GAP = 120
MIN_SHARED_BARS = 2
BAR_ALIGN_TOLERANCE = 2.5
BAR_LINE_CLUSTER = 7
SMALL_BOLD_SIZE = 9.5
AFTER_ROW_TOLERANCE = 3
REPEAT_DOTS, DOUBLE_BAR, HEAVY_BAR = 0xF07B, 0xF0D2, 0xF0D3
CUT_TIME = 0xF043
MIN_TIME_DIGITS = 2
STEM_UP, STEM_DOWN = 0xF0C7, 0xF0C8
STEM_X_TOLERANCE = 1.2
STEM_Y_REACH = 45
STEM_BEAM_CLEAR = 1.5
FLAG_X_TOLERANCE = 1.2
HEAD_DOT_MIN_DX = 2
DOT_MAX_DX = 12
HEAD_DOT_DY = 3.2
REST_DOT_DY = 6
ACCIDENTAL_MAX_DX = 11
ACCIDENTAL_DY = 1.4
ACCENT, TRILL, FERMATA = 0xF03E, 0xF0D9, 0xF055
ARTICULATION_X = 7
CHORD_MERGE_X = 1.3
TUPLET_STAFF_REACH = 45
TRIPLET_NOTES = 3
CURVE_DEDUP_Y = 2.5
TIE_LEFT_MIN_DX, TIE_LEFT_MAX_DX = -9, 2
TIE_RIGHT_MIN_DX, TIE_RIGHT_MAX_DX = -7, 9
TIE_HEAD_DY = 16
CLEF_Y_REACH = 8
SMALL_ACCIDENTAL_MIN_SIZE = 10
SMALL_ACCIDENTAL_DY = 8
TITLE_SIZE = 20
TEXT_SIZE_TOLERANCE = 0.3
CHORD_SMALL_SIZE = 9
CHORD_ROW_GAP = 6.5
TEXT_JOIN_DY = 2.5
TEXT_JOIN_MIN_DX, TEXT_JOIN_MAX_DX = -2, 7
REHEARSAL_MAX_LEN = 10
TEMPO_MIN_SIZE = 12
RUN_SPACE_GAP = 1.5
LYRIC_LABEL_DY = 20
SEGNO, CODA_SIGN = 0xF025, 0xF0DE
WEDGE_MIN_LENGTH = 10
WEDGE_JOIN = 0.5
WEDGE_FAR_ALIGN = 1.5
WEDGE_MAX_OPENING = 14
NOMINALS = {
    "black": Fraction(1),
    "x": Fraction(1),
    "circlex": Fraction(1),
    "diamond": Fraction(1),
    "filleddiamond": Fraction(1),
    "half": Fraction(2),
    "whole": Fraction(4),
}


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


@dataclasses.dataclass
class _PageContext:
    page_no: int
    staff_glyphs: dict[float, list[G]]
    spans: list[Span]
    glyphs: list[G]
    beams: list[pymupdf.Rect]
    curves: list[Any]
    lines: list[Any]
    boxes: list[Any]
    size: float
    systems: list[list[float]]


@dataclasses.dataclass
class _Staff:
    base: float
    near: list[G]
    stems: list[G]
    stem_xs: list[float]
    beams: list[pymupdf.Rect]
    size: float
    step: float


def font_map(doc: pymupdf.Document) -> dict[str, str]:
    """Recover each font's role from its glyph usage, since printed PDFs lose font names.

    Returns:
        The music role keyed by every font printing more private-use glyphs than the threshold.

    """
    counts: dict[str, dict[str, int]] = {}
    for page in doc:
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                for s in line["spans"]:
                    d = counts.setdefault(s["font"], {"pua": 0, "n": 0})
                    d["n"] += len(s["text"])
                    d["pua"] += sum(ord(c) >= PUA_START for c in s["text"])
    return {f: "music" for f, d in counts.items() if d["pua"] > MUSIC_FONT_MIN_PUA}


def _music_glyphs(s: dict[str, Any]) -> list[G]:
    glyphs: list[G] = []
    for ch in s["chars"]:
        if ord(ch["c"]) not in LEAD_SHEET_GLYPHS:
            msg = f"music glyph {ord(ch['c']):#x} is not decoded"
            raise UnknownGlyph(msg)
        glyphs.append(G(ord(ch["c"]), ch["origin"][0], ch["origin"][1], s["size"]))
    return glyphs


def _text_span(s: dict[str, Any]) -> Span | None:
    text = "".join(c["c"] for c in s["chars"])
    if not text.strip():
        return None
    flags = s["flags"]
    name = s["font"].lower()
    return Span(
        text,
        s["chars"][0]["origin"][0],
        s["origin"][1],
        s["bbox"][2],
        s["font"],
        s["size"],
        bool(flags & 16) or "bold" in name,
        bool(flags & 2) or "italic" in name,
    )


def load(page: pymupdf.Page, music: dict[str, str]) -> tuple[list[G], list[Span]]:
    glyphs: list[G] = []
    spans: list[Span] = []
    for block in page.get_text("rawdict")["blocks"]:
        for line in block.get("lines", []):
            for s in line["spans"]:
                if s["font"] in music:
                    glyphs.extend(_music_glyphs(s))
                else:
                    span = _text_span(s)
                    if span is not None:
                        spans.append(span)
    return glyphs, spans


ROOT_RE = re.compile(r"^[A-G]")
REHEARSAL = re.compile(r"Intro|Interlude|Vinahouse|[A-C]2?")


def assemble_chords(items: list[tuple[float, float, str]]) -> list[tuple[float, str]]:
    """Items are (x, x_end, text) of 1 chord row, left to right.

    Returns:
        Each assembled chord text with the x it starts at.

    """
    chords: list[tuple[float, str]] = []
    cur: list[str] = []
    cur_x = 0.0
    end = 0.0
    for x, x1, text in sorted(items):
        starts = bool(ROOT_RE.match(text)) or (text.startswith("/") and x - end > CHORD_JOIN_GAP)
        if cur and starts and not (text[0] in "ABCDEFG" and cur[-1].endswith("/") and x - end < CHORD_JOIN_GAP):
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


def voice_split(
    chords: list[Event],
    close: list[tuple[Event, Event]],
    length: Fraction,
) -> tuple[list[Event], list[Event]] | None:
    """2 voices keeping coincident events apart: 1 fills the bar and the other does not overflow.

    Returns:
        The 2 voices, or None when no split keeps the coincident events apart.

    """
    for mask in range(1, 2 ** len(chords) - 1):
        a = [e for i, e in enumerate(chords) if mask >> i & 1]
        b = [e for i, e in enumerate(chords) if not mask >> i & 1]
        if any((x in a) == (y in a) for x, y in close):
            continue
        s_a, s_b = sum((e.length for e in a), Fraction(0)), sum((e.length for e in b), Fraction(0))
        if s_a == length and s_b == length:
            return a, b
    for mask in range(1, 2 ** len(chords) - 1):
        a = [e for i, e in enumerate(chords) if mask >> i & 1]
        b = [e for i, e in enumerate(chords) if not mask >> i & 1]
        if any((x in a) == (y in a) for x, y in close):
            continue
        s_a, s_b = sum((e.length for e in a), Fraction(0)), sum((e.length for e in b), Fraction(0))
        if s_a == length and s_b <= length:
            return a, b
    return None


def stem_split(chords: list[Event], length: Fraction) -> tuple[list[Event], list[Event]] | None:
    """2 voices by stem direction, each rest going to either voice or, if stamped under a note, to neither.

    Returns:
        The up-stem and down-stem voices, or None when no assignment fills both.

    """
    up = [e for e in chords if e.heads and e.stem == "up"]
    down = [e for e in chords if e.heads and e.stem == "down"]
    rests = [e for e in chords if not e.heads]
    if not up or not down or len(up) + len(down) + len(rests) != len(chords) or len(rests) > MAX_SPLIT_RESTS:
        return None
    best = None
    for choice in itertools.product((0, 1, 2), repeat=len(rests)):
        a = up + [r for r, c in zip(rests, choice, strict=True) if c == 0]
        b = down + [r for r, c in zip(rests, choice, strict=True) if c == 1]
        dropped = [r for r, c in zip(rests, choice, strict=True) if c == REST_DROPPED]
        if any(not any(abs(r.x - e.x) < COINCIDENT_X for e in chords if e.heads) for r in dropped):
            continue
        if (
            sum((e.length for e in a), Fraction(0)) == length
            and sum((e.length for e in b), Fraction(0)) == length
            and (best is None or len(dropped) < best[0])
        ):
            best = (len(dropped), sorted(a, key=lambda e: e.x), sorted(b, key=lambda e: e.x))
    return (best[1], best[2]) if best else None


def beam_levels(centres: list[float]) -> int:
    """Beams drawn in pieces share a height; only a real gap makes another level.

    Returns:
        The number of beam levels.

    """
    levels = 0
    last = -100.0
    for y in sorted(centres):
        if y - last > BEAM_GAP:
            levels += 1
            last = y
    return levels


def nominal_dots(length: Fraction) -> tuple[Fraction, int]:
    for dots, factor in ((0, Fraction(1)), (1, Fraction(3, 2)), (2, Fraction(7, 4))):
        if length / factor in {Fraction(4), Fraction(2), Fraction(1), Fraction(1, 2), Fraction(1, 4), Fraction(1, 8)}:
            return length, dots
    msg = f"no note value for {length}"
    raise ValueError(msg)


def is_dust(text: str) -> bool:
    """Tell whether a text of only punctuation is print dust, such as a dot split off an ellipsis.

    Returns:
        True when the text is only punctuation.

    """
    return bool(re.fullmatch(r"[.,;:…·'`-]+", text))


def _merged_bases(glyphs: list[G]) -> list[float]:
    bases = sorted({round(g.y, 1) for g in glyphs if g.code == STAFF})
    merged: list[float] = []
    for b in bases:
        if not merged or b - merged[-1] > STAFF_MERGE_GAP:
            merged.append(b)
    return merged


def _staff_glyphs(glyphs: list[G], bases: list[float], size: float) -> dict[float, list[G]]:
    def owner(y: float) -> float:
        return min(bases, key=lambda b: abs(y - (b - 8.6)))

    staff_glyphs: dict[float, list[G]] = {b: [] for b in bases}
    for g in glyphs:
        if abs(g.size - size) < STAFF_SIZE_TOLERANCE or g.code == GRACE:
            staff_glyphs[owner(g.y)].append(g)
    return staff_glyphs


def _drawings(drawings: list[Any]) -> tuple[list[pymupdf.Rect], list[Any], list[Any], list[Any]]:
    beams: list[pymupdf.Rect] = []
    curves: list[Any] = []
    lines: list[Any] = []
    boxes: list[Any] = []
    for d in drawings:
        r = d["rect"]
        kinds = [i[0] for i in d["items"]]
        if (
            d.get("fill") is not None
            and "c" not in kinds
            and r.width > BEAM_MIN_WIDTH
            and 1.0 <= r.height < BEAM_MAX_HEIGHT
        ):
            if set(kinds) == {"re"}:
                beams.extend(pymupdf.Rect(i[1]) for i in d["items"])
            else:
                beams.append(r)
        elif kinds == ["c"]:
            curves.append(r)
        elif kinds == ["l"]:
            it = d["items"][0]
            lines.append((it[1].x, it[1].y, it[2].x, it[2].y))
        elif kinds == ["re"]:
            boxes.append(r)
    return beams, curves, lines, boxes


def _bar_xs(gl: list[G]) -> list[float]:
    return sorted({round(g.x, 1) for g in gl if g.code in BARS})


def _same_bars(a: list[G], b: list[G]) -> bool:
    xa, xb = _bar_xs(a), _bar_xs(b)
    return (
        len(xa) >= MIN_SHARED_BARS
        and len(xa) == len(xb)
        and all(abs(p - q) < BAR_ALIGN_TOLERANCE for p, q in zip(xa, xb, strict=False))
    )


def _systems(bases: list[float], staff_glyphs: dict[float, list[G]]) -> list[list[float]]:
    systems: list[list[float]] = []
    i = 0
    while i < len(bases):
        b = bases[i]
        if (
            i + 1 < len(bases)
            and bases[i + 1] - b < GRAND_STAFF_GAP
            and _same_bars(staff_glyphs[b], staff_glyphs[bases[i + 1]])
        ):
            systems.append([b, bases[i + 1]])
            i += 2
        else:
            systems.append([b])
            i += 1
    return systems


def _heard(sys: list[float], staff_glyphs: dict[float, list[G]], lo: float, hi: float) -> bool:
    return any(
        lo <= g.x < hi and (g.code in HEADS or g.code in RESTS or g.code in {BAR_REST, SIMILE})
        for b in sys
        for g in staff_glyphs[b]
    )


def _columns(sys: list[float], staff_glyphs: dict[float, list[G]]) -> list[tuple[float, float, set[int]]]:
    top = sys[0]
    raw = sorted((round(g.x, 1), g.code) for g in staff_glyphs[top] if g.code in BARS)
    col_list: list[tuple[float, float, set[int]]] = []
    for x, code in raw:
        if col_list and x - col_list[-1][1] < BAR_LINE_CLUSTER:
            col_list[-1] = (col_list[-1][0], x, col_list[-1][2] | {code})
        else:
            col_list.append((x, x, {code}))
    clefs = [g.x for g in staff_glyphs[top] if g.code == CLEF]
    if (
        clefs
        and col_list
        and min(clefs) < col_list[0][0] - OPENING_BAR_MIN
        and _heard(sys, staff_glyphs, min(clefs), col_list[0][0])
    ):
        col_list.insert(0, (min(clefs), min(clefs), set()))
    if len(col_list) > 1 and not _heard(sys, staff_glyphs, col_list[0][1], col_list[1][0]):
        col_list = (
            col_list[1:] if len(col_list[1][2]) > 1 or col_list[1][0] - col_list[0][1] < OPENING_BAR_MIN else col_list
        )
    return col_list


def _bounds(systems: list[list[float]], s_i: int, sys: list[float]) -> tuple[float, float, float, float]:
    top = sys[0]
    prev_base = systems[s_i - 1][-1] if s_i else None
    next_base = systems[s_i + 1][0] if s_i + 1 < len(systems) else None
    y_lo = 0 if prev_base is None else (prev_base + top) / 2 + 8
    y_hi = sys[-1] + 54 if next_base is None else (sys[-1] + next_base) / 2 + 8
    chord_lo = 0 if prev_base is None else prev_base + 2
    chord_hi = sys[-1] - 4
    return y_lo, y_hi, chord_lo, chord_hi


def _volta_or_small_bold(s: Span) -> bool:
    return bool(re.fullmatch(r"[12]\.", s.text.strip())) or (s.size < SMALL_BOLD_SIZE and s.bold)


def _rehearsal_or_after(s: Span, spans: list[Span]) -> bool:
    return any(abs(s.y - o.y) < AFTER_ROW_TOLERANCE and o.text.strip().startswith("(After") for o in spans) or bool(
        s.bold and REHEARSAL.fullmatch(s.text.strip()),
    )


def _bar_lines(bar: Bar, left: set[int], right: set[int]) -> None:
    if REPEAT_DOTS in left and HEAVY_BAR not in left:
        bar.repeat_start = True
    if REPEAT_DOTS in right and HEAVY_BAR in right:
        bar.repeat_end = True
    elif REPEAT_DOTS not in right and HEAVY_BAR in right:
        bar.final = True
    elif DOUBLE_BAR in right and REPEAT_DOTS not in right:
        bar.double = True


def _staff(base: float, near: list[G], whole_staff: list[G], beams: list[pymupdf.Rect], size: float) -> _Staff:
    stems = [g for g in whole_staff if g.code in {STEM_UP, STEM_DOWN}]
    stem_xs: list[float] = []
    for h in near:
        if h.code in HEADS:
            got = [s for s in stems if abs(s.x - h.x) < STEM_X_TOLERANCE and abs(s.y - h.y) < STEM_Y_REACH]
            if got:
                stem_xs.append(h.x + (0.02 if got[0].code == STEM_DOWN else 0.31) * size)
    return _Staff(base, near, stems, stem_xs, beams, size, STEP_PER_SIZE * size)


def _rect_width(r: pymupdf.Rect) -> float:
    return r.width


def _stem_x(g: G, stem: str | None, size: float) -> float:
    return g.x + 0.31 * size if stem == "up" else g.x + 0.02 * size


def _own_stems(g: G, kind: str, stem: str | None, stems: list[G]) -> list[G]:
    own = [s for s in stems if abs(s.x - g.x) < STEM_X_TOLERANCE and abs(s.y - g.y) < STEM_Y_REACH]
    if stem == "up" and kind in {"black", "half"}:
        own = [s for s in own if s.code == STEM_UP and s.y <= g.y + 0.6] or own
    if stem == "down" and kind in {"black", "half"}:
        own = [s for s in own if s.code == STEM_DOWN and s.y >= g.y - 0.6] or own
    return own


def _beam_level(g: G, stem: str, own: list[G], stem_x: float, staff: _Staff) -> tuple[int, int | None]:
    tips = [s.y for s in own]
    tip = (min(tips, default=g.y) - 4.3) if stem == "up" else (max(tips, default=g.y) + 4.3)
    lo, hi = sorted((g.y, tip))
    hits = [
        b
        for b in staff.beams
        if lo - 8 <= b.y0
        and b.y1 <= hi + 8
        and (
            b.x0 - 1 <= stem_x <= b.x1 + 1
            or (b.x1 <= stem_x <= b.x1 + 2.2 and not any(abs(o - b.x0) < STEM_BEAM_CLEAR for o in staff.stem_xs))
        )
    ]
    level = beam_levels([(b.y0 + b.y1) / 2 for b in hits])
    beam_id = None
    if hits:
        beam_id = id(max(hits, key=_rect_width))
    for fl in staff.near:
        if fl.code in FLAGS and abs(fl.x - g.x) < FLAG_X_TOLERANCE:
            level = max(level, FLAGS[fl.code])
    return level, beam_id


def _accidental(g: G, near: list[G]) -> str | None:
    accidental = None
    for a in near:
        if 0 < g.x - a.x < ACCIDENTAL_MAX_DX and abs(a.y - g.y) < ACCIDENTAL_DY:
            if a.code in FLATS:
                accidental = "b"
            elif a.code in SHARPS:
                accidental = "#"
            elif a.code in NATURALS:
                accidental = "n"
    return accidental


def _pitched_head(g: G, staff: _Staff, accidental: str | None, shape: str) -> Head:
    steps = round((staff.base - 0.2 - g.y) / staff.step)
    index = 2 + steps + 4 * 7
    return Head(g.x, g.y, LETTERS[index % 7], index // 7, accidental, shape)


def _head_event(g: G, bar_length: Fraction, staff: _Staff) -> Event:
    kind, stem = HEADS[g.code]
    own = _own_stems(g, kind, stem, staff.stems)
    if stem is None or kind == "whole":
        stem = None
    nominal = NOMINALS[kind]
    stem_x = _stem_x(g, stem, staff.size)
    if kind == "half" and not own:
        nominal = bar_length
        stem = None
    if own:
        stem = "down" if own[0].code == STEM_DOWN else "up"
        stem_x = _stem_x(g, stem, staff.size)
    beam_id = None
    if stem is not None and stem != "":
        level, beam_id = _beam_level(g, stem, own, stem_x, staff)
        if level and kind in {"black", "x", "circlex", "diamond", "filleddiamond"}:
            nominal /= 2**level
    near = staff.near
    dots = int(
        any(d.code == DOT and HEAD_DOT_MIN_DX < d.x - g.x < DOT_MAX_DX and abs(d.y - g.y) < HEAD_DOT_DY for d in near),
    )
    if dots:
        nominal *= Fraction(3, 2)
    shape = kind if kind in {"x", "circlex", "diamond", "filleddiamond"} else "normal"
    head = _pitched_head(g, staff, _accidental(g, near), shape)
    ev = Event(g.x, [head], nominal, dots, stem, beam=beam_id)
    ev.accent = any(a.code == ACCENT and abs(a.x - g.x) < ARTICULATION_X for a in near)
    ev.trill = any(t.code == TRILL and abs(t.x - g.x) < ARTICULATION_X for t in near)
    ev.fermata = any(t.code == FERMATA and abs(t.x - g.x) < ARTICULATION_X for t in near)
    return ev


def _rest_event(g: G, near: list[G]) -> Event:
    length = RESTS[g.code]
    dots = int(any(d.code == DOT and 1 < d.x - g.x < DOT_MAX_DX and abs(d.y - g.y) < REST_DOT_DY for d in near))
    if dots:
        length *= Fraction(3, 2)
    return Event(g.x, [], length, dots)


def _merge_chords(events: list[Event]) -> list[Event]:
    chords: list[Event] = []
    for ev in events:
        for other in chords:
            if (
                abs(other.x - ev.x) < CHORD_MERGE_X
                and ev.heads
                and other.heads
                and other.nominal == ev.nominal
                and other.stem == ev.stem
            ):
                other.heads += ev.heads
                break
        else:
            chords.append(ev)
    for ev in chords:
        ev.heads = list({(h.step, h.octave): h for h in ev.heads}.values())
        ev.heads.sort(key=lambda h: -h.y)
    return chords


def _mark_beamed_triplets(bar: Bar, chords: list[Event]) -> None:
    groups: dict[int, list[Event]] = {}
    for ev in chords:
        if ev.beam is not None:
            groups.setdefault(ev.beam, []).append(ev)
    triple = [g for g in groups.values() if len(g) == TRIPLET_NOTES and len({e.nominal for e in g}) == 1]
    if (
        triple
        and sum((e.length for e in chords), Fraction(0))
        - sum((sum((e.length for e in g), Fraction(0)) / 3 for g in triple), Fraction(0))
        == bar.length
    ):
        for g in triple:
            for ev in g:
                ev.tuplet = True


def _carry_ties(flat: list[Event]) -> None:
    for a, b in itertools.pairwise(flat):
        for h in a.heads:
            if h.tie_start:
                for q in b.heads:
                    if q.step == h.step and q.octave == h.octave:
                        q.tie_stop = True
                        q.alter = h.alter


def _number_like(s: Span, clef_x: float) -> bool:
    return (
        abs(s.size - 8.0) < TEXT_SIZE_TOLERANCE
        and s.text.strip().isdigit()
        and (not s.font.startswith("Times") or s.x < clef_x)
    )


def _near_bar_start(s: Span, bars: list[Bar], sys: list[float]) -> bool:
    return bool(bars) and s.y > sys[0] - 45 and s.x < bars[0].left + 40


def _chord_rows(allitems: list[Span]) -> list[list[Span]]:
    groups: list[list[Span]] = []
    for s in allitems:
        for g in groups:
            if abs(g[0].y - s.y) < CHORD_ROW_GAP:
                g.append(s)
                break
        else:
            groups.append([s])
    return groups


def _text_groups(spans: list[Span]) -> list[list[Span]]:
    groups: list[list[Span]] = []
    for s in sorted(spans, key=lambda s: s.x):
        for g in groups:
            if abs(g[-1].y - s.y) < TEXT_JOIN_DY and TEXT_JOIN_MIN_DX < s.x - g[-1].x1 < TEXT_JOIN_MAX_DX:
                g.append(s)
                break
        else:
            groups.append([s])
    return groups


def _rehearsal_text(first: Span, text: str) -> bool:
    return (first.size < SMALL_BOLD_SIZE and len(text) <= REHEARSAL_MAX_LEN) or bool(REHEARSAL.fullmatch(text))


def _is_rehearsal(first: Span, sys: list[float], text: str) -> bool:
    return first.bold and first.y < sys[0] - 10 and "(" not in text and _rehearsal_text(first, text)


def _apart(prev: str, piece: str, gap: float) -> bool:
    return gap > RUN_SPACE_GAP or (piece[0].isalnum() and prev[-1].isalnum())


def _joins_apart(prev: str, piece: str, gap: float) -> bool:
    return bool(piece) and bool(prev) and _apart(prev, piece, gap) and not piece.startswith(" ") and not prev.endswith(" ")


def _runs(group: list[Span]) -> list[tuple[str, bool, bool]]:
    runs: list[tuple[str, bool, bool]] = []
    for i, s in enumerate(group):
        piece = s.text.strip() if len(group) == 1 else s.text
        if i and _joins_apart(runs[-1][0], piece, s.x - group[i - 1].x1):
            piece = " " + piece
        runs.append((piece, s.bold, s.italic))
    label_at = next((i for i, run in enumerate(runs) if i and ":" in run[0] and run[1] and run[2]), 0)
    if label_at and not any(":" in run[0] for run in runs[:label_at]):
        head = runs[label_at:]
        runs = [
            (head[0][0].strip(), head[0][1], head[0][2]),
            *head[1:],
            ("\n" + runs[0][0], runs[0][1], runs[0][2]),
            *runs[1:label_at],
        ]
    return runs


def _attach(target: list[Text], t: Text, first: Span, runs: list[tuple[str, bool, bool]]) -> None:
    lyric = [o for o in target if o.runs[0][1] and o.runs[0][2] and ":" in o.runs[0][0]]
    if lyric and first.bold and first.italic and ":" in runs[0][0] and abs(lyric[0].y - first.y) < LYRIC_LABEL_DY:
        lyric[0].runs += [("\n" + runs[0][0], runs[0][1], runs[0][2]), *runs[1:]]
    else:
        cue = [
            o
            for o in target
            if first.bold
            and first.italic
            and ":" in runs[0][0]
            and o.runs[0][1]
            and o.runs[0][2]
            and abs(o.y - first.y) < LYRIC_LABEL_DY
        ]
        for o in cue:
            target.remove(o)
            t.runs += [("\n" + o.runs[0][0], o.runs[0][1], o.runs[0][2]), *o.runs[1:]]
        target.append(t)


class Decoder:
    def __init__(self, path: str, key_hint: int | None = None) -> None:
        self.doc = pymupdf.open(path)
        self.music = font_map(self.doc)
        self.bars: list[Bar] = []
        self.corrections: list[Issue] = []
        self.flags: list[Issue] = []
        self.time = (4, 4)
        self.time_len = Fraction(4)
        self.key = 0
        self.second = False
        self.first = True
        self.prev_chord: Chord | None = None
        self.open_ties: list[Head] = []
        self.printed: list[tuple[int, int]] = []

    def run(self) -> Score:
        for page_no, page in enumerate(self.doc.pages()):
            self._page(page_no, page)
        self._finish_voltas()
        for ours, theirs in self.printed:
            if ours != theirs:
                self.flags.append(Issue(ours, f"printed bar number {theirs}"))
        return Score(self.bars, self.corrections, self.flags, self.second)

    def _finish_voltas(self) -> None:
        for i, bar in enumerate(self.bars):
            if bar.volta is not None and bar.volta != "":
                j = i
                if bar.volta == "1":
                    while j + 1 < len(self.bars) and not self.bars[j].repeat_end:
                        j += 1
                self.bars[j].volta_stop = bar.volta

    def _page(self, page_no: int, page: pymupdf.Page) -> None:
        glyphs, spans = load(page, self.music)
        self.threes = [(g.x, g.y) for g in glyphs if g.code == DIGIT_THREE and g.size < TUPLET_GLYPH_MAX_SIZE]
        self.threes += [(s.x, s.y) for s in spans if s.text.strip() == "3" and s.size > TUPLET_TEXT_MIN_SIZE]
        spans = [s for s in spans if not (s.text.strip() == "3" and s.size > TUPLET_TEXT_MIN_SIZE)]
        bases = _merged_bases(glyphs)
        size = next(g.size for g in glyphs if g.code == STAFF)
        staff_glyphs = _staff_glyphs(glyphs, bases, size)
        beams, curves, lines, boxes = _drawings(page.get_drawings())
        systems = _systems(bases, staff_glyphs)
        ctx = _PageContext(page_no, staff_glyphs, spans, glyphs, beams, curves, lines, boxes, size, systems)
        first_on_page = True
        for s_i, sys in enumerate(systems):
            self._system(ctx, sys, s_i, first_on_page=first_on_page)
            first_on_page = False

    def _system(self, ctx: _PageContext, sys: list[float], s_i: int, *, first_on_page: bool) -> None:
        top = sys[0]
        col_list = _columns(sys, ctx.staff_glyphs)
        {c[0]: c[2] for c in col_list}
        pairs = [(a, b) for a, b in itertools.pairwise(col_list)]
        y_lo, y_hi, chord_lo, chord_hi = _bounds(ctx.systems, s_i, sys)
        bounds = (chord_lo, chord_hi, top, y_lo, y_hi)
        my_spans = [s for s in ctx.spans if self._mine(s, ctx.spans, bounds)]
        new_bars = self._build_bars(ctx, sys, pairs, first_on_page=first_on_page)
        self._keysig(new_bars, ctx.staff_glyphs[top])
        self._curves(new_bars, sys, ctx.curves)
        self._apply_pitch(new_bars)
        self._chords_and_text(new_bars, sys, my_spans, ctx, s_i)
        self._signs(new_bars, ctx.glyphs, chord_lo, top + 5)
        self._wedges(new_bars, ctx.lines, max(top - 40, y_lo), y_hi)
        self._volta(new_bars, my_spans)

    def _mine(self, s: Span, spans: list[Span], bounds: tuple[float, float, float, float, float]) -> bool:
        chord_lo, chord_hi, top, y_lo, y_hi = bounds
        if self._is_chord(s):
            return chord_lo <= s.y < chord_hi
        if _volta_or_small_bold(s) or _rehearsal_or_after(s, spans):
            return chord_lo <= s.y < top
        return y_lo <= s.y < y_hi

    def _build_bars(
        self,
        ctx: _PageContext,
        sys: list[float],
        pairs: list[tuple[tuple[float, float, set[int]], tuple[float, float, set[int]]]],
        *,
        first_on_page: bool,
    ) -> list[Bar]:
        top = sys[0]
        new_bars: list[Bar] = []
        for p_i, (col_a, col_b) in enumerate(pairs):
            left, right = col_a[1], col_b[0]
            bar = Bar(len(self.bars) + 1, ctx.page_no, left, right, self.time_len)
            bar.base = top
            bar.new_system = p_i == 0 and bool(self.bars)
            bar.new_page = p_i == 0 and first_on_page and bool(self.bars)
            _bar_lines(bar, col_a[2], col_b[2])
            self._bar_contents(bar, sys, ctx.staff_glyphs, ctx.beams, ctx.size)
            self.bars.append(bar)
            new_bars.append(bar)
        return new_bars

    def _keysig(self, bars: list[Bar], top: list[G]) -> None:
        if not bars or self.first is False:
            return
        first = bars[0]
        flats = sum(g.code in FLATS and first.left < g.x < first.left + 60 for g in top)
        sharps = sum(g.code in SHARPS and first.left < g.x < first.left + 60 for g in top)
        self.key = sharps - flats
        first.key = self.key
        self.first = False

    def _bar_contents(
        self,
        bar: Bar,
        sys: list[float],
        staff_glyphs: dict[float, list[G]],
        beams: list[pymupdf.Rect],
        size: float,
    ) -> None:
        self.top_base = sys[0]
        for idx, base in enumerate(sys):
            self.current_base = base
            near = [g for g in staff_glyphs[base] if bar.left <= g.x < bar.right - 0.5]
            if idx == 0:
                self._time_change(bar, near)
            voices = self._read_staff(bar, _staff(base, near, staff_glyphs[base], beams, size))
            if idx == 0:
                bar.voices = voices
            else:
                bar.voices2 = voices
                self.second = True

    def _time_change(self, bar: Bar, near: list[G]) -> None:
        digits = sorted(
            (g for g in near if DIGIT_ONE <= g.code <= DIGIT_NINE and abs(g.size - 17.9) < 1),
            key=lambda g: g.y,
        )
        sym = [g for g in near if g.code == CUT_TIME]
        new = None
        if sym:
            new = (4, 4)
            bar.time_symbol = "cut"
        elif len(digits) >= MIN_TIME_DIGITS:
            new = (digits[0].code - 0x30, digits[-1].code - 0x30)
        if new:
            bar.time = new
            self.time = new
            self.time_len = Fraction(new[0] * 4, new[1])
            bar.length = self.time_len

    def _read_staff(self, bar: Bar, staff: _Staff) -> list[list[Event]]:
        events: list[Event] = []
        bar_rest = False
        for g in staff.near:
            if g.code in HEADS:
                events.append(_head_event(g, bar.length, staff))
            elif g.code == GRACE:
                head = _pitched_head(g, staff, None, "normal")
                events.append(Event(g.x, [head], Fraction(1, 2), 0, "up", grace=True))
            elif g.code in RESTS:
                events.append(_rest_event(g, staff.near))
            elif g.code == BAR_REST:
                bar_rest = True
            elif g.code == SIMILE:
                t = Text(g.x, g.y, [("//", True, False)], above=True, size=10)
                (bar.texts if staff.base == self.top_base else bar.texts2).append(t)
        events.sort(key=lambda e: e.x)
        return self.voices(bar, events, bar_rest, staff.near)

    def voices(self, bar: Bar, events: list[Event], bar_rest: bool, near: list[G]) -> list[list[Event]]:
        chords = _merge_chords(events)
        self._mark_threes(bar, chords)
        if not chords:
            return [[Event(bar.left, [], bar.length, bar_rest=True)]]
        rests_only = all(not e.heads for e in chords)
        total = sum((e.length for e in chords), Fraction(0))
        if total == bar.length:
            return [self._timed(chords)]
        self._mark_short(bar, chords)
        _mark_beamed_triplets(bar, chords)
        total = sum((e.length for e in chords), Fraction(0))
        if total == bar.length:
            return [self._timed(chords)]
        return self._settle(bar, chords, total, rests_only=rests_only)

    def _mark_threes(self, bar: Bar, chords: list[Event]) -> None:
        for tx, ty in self.threes:
            if bar.left <= tx < bar.right and chords and abs(ty - self.current_base) < TUPLET_STAFF_REACH:
                for ev in sorted(chords, key=lambda e: abs(e.x - tx))[:3]:
                    ev.tuplet = True

    def _settle(self, bar: Bar, chords: list[Event], total: Fraction, *, rests_only: bool) -> list[list[Event]]:
        split = self._layout_split(bar, chords)
        if split is not None:
            return split
        if rests_only and all(e.nominal == chords[0].nominal for e in chords) and len(chords) > 1:
            return self._even_rests(bar, chords)
        if total < bar.length and not chords[-1].heads:
            return self._close_rest(bar, chords, total)
        repaired = self._repair(bar, chords, total)
        if repaired is not None and repaired:
            return repaired
        self.flags.append(
            Issue(bar.number, f"bar reads {total} quarters against {bar.length}: " + " ".join(self._names(chords))),
        )
        return [self._pad(bar, chords)]

    def _layout_split(self, bar: Bar, chords: list[Event]) -> list[list[Event]] | None:
        up = [e for e in chords if e.stem == "up"]
        down = [e for e in chords if e.stem == "down"]
        free = [e for e in chords if e.stem is None]
        for layout in ((up + free, down), (up, down + free), (free, up + down)):
            a = sorted(layout[0], key=lambda e: e.x)
            b = sorted(layout[1], key=lambda e: e.x)
            if a and b:
                s_a, s_b = sum((e.length for e in a), Fraction(0)), sum((e.length for e in b), Fraction(0))
                if (s_a == bar.length and s_b <= bar.length) or (s_b == bar.length and s_a <= bar.length):
                    return self._two_voices(bar, a, b)
        return None

    def _even_rests(self, bar: Bar, chords: list[Event]) -> list[list[Event]]:
        each = bar.length / len(chords)
        self.corrections.append(
            Issue(
                bar.number,
                f"{len(chords)} rests of {chords[0].nominal} quarters cannot fill the {bar.length}-quarter bar; "
                f"written as {each} each",
            ),
        )
        for ev in chords:
            ev.nominal, ev.dots = nominal_dots(each)
        return [self._timed(chords)]

    def _close_rest(self, bar: Bar, chords: list[Event], total: Fraction) -> list[list[Event]]:
        new = chords[-1].length + bar.length - total
        self.corrections.append(
            Issue(
                bar.number,
                f"bar printed {total} quarters of {bar.length}; closing rest {chords[-1].length} written as {new}",
            ),
        )
        chords[-1].nominal, chords[-1].dots = nominal_dots(new)
        return [self._timed(chords)]

    def _split_voices(self, bar: Bar, chords: list[Event]) -> list[list[Event]] | None:
        by_stem = stem_split(chords, bar.length)
        if by_stem:
            return self._two_voices(bar, *by_stem)
        close = [(a, b) for i, a in enumerate(chords) for b in chords[i + 1 :] if abs(a.x - b.x) < COINCIDENT_X]
        if close and len(chords) <= MAX_SPLIT_EVENTS:
            split = voice_split(chords, close, bar.length)
            if split:
                if all(not e.heads for e in split[1]):
                    return [self._timed(split[0])]
                return self._two_voices(bar, *split)
        return None

    def _repair(self, bar: Bar, chords: list[Event], total: Fraction) -> list[list[Event]] | None:
        """Repair bars the printed values cannot fill, with the bar length as the evidence.

        Each repair but a voice split is listed.

        Returns:
            The repaired voices, or None when no repair fits the bar.

        """
        split = self._split_voices(bar, chords)
        if split is not None:
            return split
        if all(e.nominal == Fraction(1, 2) for e in chords) and len(chords) * Fraction(1, 3) == bar.length:
            for ev in chords:
                ev.tuplet = True
            return [self._timed(chords)]
        shortest = min((e.length for e in chords if e.heads), default=Fraction(0))
        tiny = [e for e in chords if e.heads and e.length == shortest]
        if total < bar.length and len(tiny) > 1 and sum((e.length for e in tiny), Fraction(0)) == bar.length - total:
            self.corrections.append(
                Issue(
                    bar.number,
                    f"bar printed {total} quarters of {bar.length}; notes of {shortest} written as {shortest * 2}",
                ),
            )
            for ev in tiny:
                ev.nominal *= 2
            return [self._timed(chords)]
        last = chords[-1]
        new = last.length + bar.length - total
        if last.heads and new in REPAIR_LENGTHS and not last.tuplet:
            self.corrections.append(
                Issue(
                    bar.number,
                    f"bar printed {total} quarters of {bar.length}; last note {last.length} written as {new}",
                ),
            )
            last.nominal, last.dots = nominal_dots(new)
            return [self._timed(chords)]
        return None

    @staticmethod
    def _mark_short(bar: Bar, chords: list[Event]) -> None:
        """Fill what the bar leaves with a circled x, which carries no length of its own."""
        unknown = [e for e in chords if e.heads and e.heads[0].shape == "circlex"]
        total = sum((e.length for e in chords), Fraction(0))
        if len(unknown) == 1 and total < bar.length:
            ev = unknown[0]
            new = ev.length + bar.length - total
            if new in {Fraction(1), Fraction(2), Fraction(4), Fraction(3, 2), Fraction(3), Fraction(1, 2)}:
                ev.dots = 1 if new in {Fraction(3, 2), Fraction(3)} else 0
                ev.nominal = new

    @staticmethod
    def _names(chords: list[Event]) -> list[str]:
        return [("+".join(h.step + str(h.octave) for h in e.heads) or "rest") + ":" + str(e.length) for e in chords]

    @staticmethod
    def _timed(voice: list[Event]) -> list[Event]:
        t = Fraction(0)
        for ev in voice:
            ev.onset = t
            t += ev.length
        return voice

    def _pad(self, bar: Bar, chords: list[Event]) -> list[Event]:
        voice = self._timed(chords)
        total = sum((e.length for e in voice), Fraction(0))
        if total < bar.length:
            voice += self._fill(bar.length - total, chords[-1].x + 1, total)
        return voice

    @staticmethod
    def _fill(gap: Fraction, x: float, onset: Fraction) -> list[Event]:
        out = []
        for size in (
            Fraction(4),
            Fraction(3),
            Fraction(2),
            Fraction(3, 2),
            Fraction(1),
            Fraction(3, 4),
            Fraction(1, 2),
            Fraction(1, 4),
            Fraction(1, 8),
        ):
            while gap >= size:
                dots = 1 if size in {Fraction(3), Fraction(3, 2), Fraction(3, 4)} else 0
                out.append(Event(x, [], size, dots, hidden=True, onset=onset))
                onset += size
                gap -= size
        return out

    def _two_voices(self, bar: Bar, a: list[Event], b: list[Event]) -> list[list[Event]]:
        s_a = sum((e.length for e in a), Fraction(0))
        if s_a == bar.length and sum((e.length for e in b), Fraction(0)) == bar.length:
            return [self._timed(a), self._timed(b)]
        full, other = (a, b) if s_a == bar.length else (b, a)
        self._timed(full)
        anchors = [(e.x, e.onset) for e in full]
        out = []
        t = Fraction(0)
        for ev in other:
            onset = min(anchors, key=lambda p: abs(p[0] - ev.x))[1]
            if onset > t:
                out += self._fill(onset - t, ev.x - 1, t)
                t = onset
            ev.onset = t
            out.append(ev)
            t += ev.length
        if t < bar.length:
            out += self._fill(bar.length - t, other[-1].x + 1, t)
        return [a if a is full else out, out if a is full else b] if False else ([full, out] if a is full else [out, full])

    def _curves(self, bars: list[Bar], sys: list[float], curves: list[Any]) -> None:
        seen: list[tuple[float, float, float]] = []
        all_ev = [
            (ev, bar)
            for bar in bars
            for voices in (bar.voices, bar.voices2)
            for voice in voices
            for ev in voice
            if ev.heads
        ]
        unique = []
        for r in curves:
            key = (r.x0, r.x1, r.y0)
            if any(abs(k[0] - key[0]) < 1 and abs(k[1] - key[1]) < 1 and abs(k[2] - key[2]) < CURVE_DEDUP_Y for k in seen):
                continue
            seen.append(key)
            unique.append(r)
        for r in unique:
            mid = (r.y0 + r.y1) / 2
            if not any(s - 40 < mid < s + 24 for s in sys):
                continue
            left = [
                e
                for e, _ in all_ev
                if TIE_LEFT_MIN_DX <= e.x - r.x0 <= TIE_LEFT_MAX_DX and any(abs(h.y - mid) < TIE_HEAD_DY for h in e.heads)
            ]
            right = [
                e
                for e, _ in all_ev
                if TIE_RIGHT_MIN_DX <= e.x - r.x1 <= TIE_RIGHT_MAX_DX
                and e.x > r.x0 + 2
                and any(abs(h.y - mid) < TIE_HEAD_DY for h in e.heads)
            ]
            if not left:
                continue
            l_ev = min(left, key=lambda e: abs(e.x - r.x0))
            if not right:
                l_ev.heads[0].tie_start = True
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

    def _key_default(self, step: str) -> int:
        order = "FCGDAEB"
        default = 0
        if self.key > 0 and order.index(step) < self.key:
            default = 1
        if self.key < 0 and order[::-1].index(step) < -self.key:
            default = -1
        return default

    def _alter_voices(self, voices: list[list[Event]]) -> None:
        state: dict[tuple[str, int], int] = {}
        for voice in voices:
            for ev in voice:
                for h in ev.heads:
                    key = (h.step, h.octave)
                    if h.accidental is not None and h.accidental != "":
                        state[key] = {"b": -1, "#": 1, "n": 0}[h.accidental]
                    h.alter = state.get(key, self._key_default(h.step))

    def _apply_pitch(self, bars: list[Bar]) -> None:
        for bar in bars:
            for voices in (bar.voices, bar.voices2):
                self._alter_voices(voices)
        flat = [ev for bar in self.bars for voice in bar.voices[:1] for ev in voice if ev.heads]
        _carry_ties(flat)

    def _chords_and_text(
        self,
        bars: list[Bar],
        sys: list[float],
        spans: list[Span],
        ctx: _PageContext,
        s_i: int,
    ) -> None:
        clef_x = min((g.x + 1 for g in ctx.glyphs if g.code == CLEF and abs(g.y - sys[0]) < CLEF_Y_REACH), default=0.0)
        small_flats = [
            g
            for g in ctx.glyphs
            if g.code in FLATS | SHARPS
            and abs(g.size - ctx.size) > STAFF_SIZE_TOLERANCE
            and g.size > SMALL_ACCIDENTAL_MIN_SIZE
        ]
        items = self._text_items(bars, sys, spans, clef_x)
        used = {id(s) for s in items if self._is_chord(s)}
        allitems = sorted((s for s in items if id(s) in used), key=lambda s: (s.size < CHORD_SMALL_SIZE, s.y))
        self._place_chords(bars, _chord_rows(allitems), small_flats)
        self._place_texts(bars, sys, [s for s in items if id(s) not in used], s_i)

    def _text_items(self, bars: list[Bar], sys: list[float], spans: list[Span], clef_x: float) -> list[Span]:
        items = []
        credit_rows = [(s.y, s.x) for s in spans if any(n in s.text for n in NOISE)]
        for s in spans:
            if any(n in s.text for n in NOISE) or s.size > TITLE_SIZE:
                continue
            if (
                bars
                and bars[0].number == 1
                and (s.y < MARK_Y or any(abs(s.y - y) < CREDIT_ROW and s.x >= x for y, x in credit_rows))
            ):
                continue
            if _number_like(s, clef_x) and _near_bar_start(s, bars, sys):
                self.printed.append((bars[0].number, int(s.text)))
                continue
            items.append(s)
        return items

    def _place_chords(self, bars: list[Bar], groups: list[list[Span]], small_flats: list[G]) -> None:
        for g in groups:
            ys = g[0].y
            pieces = [(s.x, s.x1, s.text.strip()) for s in g]
            pieces.extend(
                (fl.x, fl.x + 3, "b" if fl.code in FLATS else "#")
                for fl in small_flats
                if abs(fl.y - ys + 5) < SMALL_ACCIDENTAL_DY
            )
            for x, text in assemble_chords(pieces):
                chord = parse_chord(self._fix_accidentals(text), self.prev_chord, x)
                if chord is None:
                    self.flags.append(Issue(bars[0].number if bars else 0, f"unread chord {text!r}"))
                    continue
                bar = next((b for b in bars if x < b.right - 11), bars[-1] if bars else None)
                if bar is None:
                    continue
                bar.chords.append(chord)
                self.prev_chord = chord

    @staticmethod
    def _fix_accidentals(text: str) -> str:
        return text

    def _is_chord(self, s: Span) -> bool:
        text = s.text.strip()
        if s.bold or s.italic:
            return False
        if abs(s.size - 8.0) < TEXT_SIZE_TOLERANCE:
            return bool(re.fullmatch(r"[0-9()#badd♭♯susmajdim+/]+", text))
        if abs(s.size - 10.8) < TEXT_SIZE_TOLERANCE:
            return bool(
                re.fullmatch(r"(/?[A-G](b|#)?(maj|m|dim|sus|add|aug)?[0-9()#b]*(/[A-G]?)?|maj|m|sus|dim)", text),
            ) and self._chord_font(s)
        return False

    @staticmethod
    def _chord_font(s: Span) -> bool:
        return ("Verdana" not in s.font and not s.font.endswith("FPEF")) or s.font.startswith("Times")

    def _place_texts(self, bars: list[Bar], sys: list[float], spans: list[Span], s_i: int) -> None:
        if not bars:
            return
        for group in sorted(_text_groups(spans), key=lambda g: g[0].y):
            self._place_text(bars, sys, spans, group, s_i)

    def _place_text(self, bars: list[Bar], sys: list[float], spans: list[Span], group: list[Span], s_i: int) -> None:
        first = group[0]
        text = "".join(s.text for s in group).strip()
        if not text or is_dust(text):
            return
        if _is_rehearsal(first, sys, text):
            bar_at(bars, first.x, 15).rehearsal.append(text)
            return
        if re.fullmatch(r"[12]\.", text):
            return
        if first.size > TEMPO_MIN_SIZE and first.bold and first.italic and s_i == 0:
            bars[0].tempo = (re.sub(r"\s*=.*$", "", text), self.tempo_value(spans))
            return
        if re.fullmatch(r"=?\s*\d+", text.replace(" ", "")) or text.startswith("="):
            return
        base = min(sys, key=lambda b: abs(first.y - (b - 8.6)))
        bar = next((b for b in bars if first.x < b.right - 2), bars[-1])
        runs = _runs(group)
        t = Text(first.x, first.y, runs, first.y < base - 8, size=first.size * 0.9)
        _attach(bar.texts if base == sys[0] else bar.texts2, t, first, runs)

    @staticmethod
    def tempo_value(spans: list[Span]) -> float:
        whole = " ".join(s.text for s in spans)
        values = [float(m.replace(" ", "")) for m in re.findall(r"=\s*(\d+(?:\s*\.\s*\d+)?)", whole)]
        if not values:
            msg = "no tempo printed"
            raise ValueError(msg)
        return min(values)

    @staticmethod
    def _signs(bars: list[Bar], glyphs: list[G], y_lo: float, y_hi: float) -> None:
        if not bars:
            return
        span = bars[-1].right - bars[0].left
        for g in glyphs:
            if not y_lo <= g.y < y_hi:
                continue
            if g.code in {SEGNO, CODA_SIGN}:
                left = g.x < bars[0].left + span * 0.4
                if g.code == SEGNO:
                    if left:
                        bars[0].segno = True
                    else:
                        bars[-1].dal_segno = True
                elif left:
                    bars[0].coda = True
                else:
                    bars[-1].to_coda = True

    @staticmethod
    def _wedges(bars: list[Bar], lines: list[Any], y_lo: float, y_hi: float) -> None:
        if not bars:
            return
        cand = [seg for seg in lines if y_lo < seg[1] < y_hi and abs(seg[2] - seg[0]) > WEDGE_MIN_LENGTH]
        for i, a in enumerate(cand):
            for b in cand[i + 1 :]:
                if abs(a[2] - b[0]) < WEDGE_JOIN and abs(a[3] - b[1]) < WEDGE_JOIN:
                    far_gap = abs(a[1] - b[3])
                    if abs(a[0] - b[2]) > WEDGE_FAR_ALIGN or not 1 < far_gap < WEDGE_MAX_OPENING:
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

    @staticmethod
    def _volta(bars: list[Bar], spans: list[Span]) -> None:
        for s in spans:
            text = s.text.strip()
            if re.fullmatch(r"[12]\.", text) and s.bold:
                bar = bar_at(bars, s.x, 4) if bars else None
                if bar:
                    bar.volta = text[0]


def decode(path: str) -> Score:
    return Decoder(path).run()
