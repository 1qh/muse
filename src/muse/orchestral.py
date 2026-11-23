"""Decode an orchestral full score in the symbol-font encoding.

Every staff of every system is read, each named by its own printed label.

Events are built around stems, not heads: a stem with every head on it is 1 chord, a beamed figure belongs to the staff its
beam serves, voices part only where 2 stems or heads coincide, and a bracketed 3 or 5 scales every stem and rest under it.
"""

from __future__ import annotations

import collections
import dataclasses
import itertools
import operator
from fractions import Fraction as F
from typing import TYPE_CHECKING, Any

import pymupdf

from muse import symbolfont
from muse.model import LETTERS, Bar, Clef, Event, Head, Orchestra, Part, Text, Transposition, UnknownGlyph
from muse.smufl import beam_y
from muse.symbolfont import (
    ACCENT_BELOW,
    ALTO_CLEF,
    AUGMENTATION_DOT,
    BASS_CLEF,
    BRACKET_BOTTOM,
    BRACKET_TOP,
    DIAMOND_WHOLE,
    DYNAMIC_LETTERS,
    OCTAVE_BELOW,
    TEXT_NATURAL,
    TIME_FOUR,
)

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Callable, Iterator

FOLDER = "Orchestral"


class Unreadable(ValueError):
    """A full score the reading rules cannot read whole; guessing would ship wrong music."""


BLACK, HALF, WHOLE = 0xF0CF, 0xF0FA, 0xF077
HEADS = {BLACK: F(1), HALF: F(2), WHOLE: F(4), DIAMOND_WHOLE: F(4)}
BAR_REST = symbolfont.BAR_REST
RESTS = {BAR_REST: None, 0xF0EE: F(2), 0xF0CE: F(1), 0xF0E4: F(1, 2)}
UP_FLAG, DOWN_FLAG = 0xF06A, 0xF04A
TREBLE_CLEF = symbolfont.CLEF
SHARP, FLAT, NATURAL = 0xF023, 0xF062, 0xF06E
FERMATA, TRILL = 0xF055, 0xF0D9
BRACE, TREMOLO = 0xF07B, 0xF03D
TEXT_SHARP, TEXT_FLAT, TEXT_QUARTER = 0x23, 0x62, 0x71
TUPLET = {0x33: F(2, 3), 0x35: F(4, 5)}
SYMBOL_FONT_START = 0xF000
MUSIC_MEANINGS = {
    *HEADS,
    *RESTS,
    UP_FLAG,
    DOWN_FLAG,
    TREBLE_CLEF,
    BASS_CLEF,
    ALTO_CLEF,
    TIME_FOUR,
    SHARP,
    FLAT,
    NATURAL,
    FERMATA,
    TRILL,
    *(
        code
        for code in symbolfont.ORCHESTRAL_GLYPHS
        if code >= SYMBOL_FONT_START
        and code not in {AUGMENTATION_DOT, BRACKET_TOP, BRACKET_BOTTOM, symbolfont.ARPEGGIO, OCTAVE_BELOW, DIAMOND_WHOLE}
    ),
}
EXTRA_MEANINGS = {
    BRACE,
    TREMOLO,
    DIAMOND_WHOLE,
    AUGMENTATION_DOT,
    BRACKET_TOP,
    BRACKET_BOTTOM,
    symbolfont.ARPEGGIO,
    OCTAVE_BELOW,
}
TEXT_MEANINGS = {*DYNAMIC_LETTERS, TEXT_SHARP, TEXT_FLAT, TEXT_NATURAL, TEXT_QUARTER, *TUPLET}
TEXT_SPELLING = {TEXT_SHARP: "♯", TEXT_FLAT: "♭", TEXT_NATURAL: "♮", TEXT_QUARTER: "♩"}
FONT_MEANINGS = {"music": MUSIC_MEANINGS, "extra": EXTRA_MEANINGS, "symbols": TEXT_MEANINGS}

STEM_WIDTH = 0.3
SYSTEM_LINE = 0.36
STAFF_LINE_MIN = 60.0
LABEL_REACH = 4
CLEF_LINES = {TREBLE_CLEF: ("G", 32), BASS_CLEF: ("F", 24), ALTO_CLEF: ("C", 28)}
KEY_ACCIDENTALS = {SHARP: 1, FLAT: -1}
ACCIDENTALS = {SHARP: "#", FLAT: "b", NATURAL: "n"}
ACCIDENTAL_REACH = 7.0
KEY_START = 12.0
KEY_SPACING = 4.5
ARTICULATION_REACH = 4.0
HAIRPIN_RISE = (0.5, 9.0)
HAIRPIN_MIN = 10.0
HAIRPIN_JOIN = 1.0
HAIRPIN_OPEN = 1.5
ARPEGGIO_REACH = 12.0
NOTE_MARKS = {
    symbolfont.TENUTO: "tenuto",
    symbolfont.MARCATO: "strong-accent-up",
    symbolfont.MARCATO_BELOW: "strong-accent-down",
    symbolfont.UP_BOW: "up-bow",
    symbolfont.DOWN_BOW: "down-bow",
    symbolfont.HARMONIC: "harmonic",
    symbolfont.ARPEGGIO: "arpeggiate",
    TREMOLO: "tremolo",
}
MARK_REACH_ABOVE = 40.0
MARK_REACH_BELOW = 30.0
WORD_GAP = 3.0
HEADER_REACH = 12.0
STRAIGHT_TOLERANCE = 0.1
TICK_MIN_WIDTH = 2
BEAM_MAX_HEIGHT = 2
BEAM_MIN_WIDTH = 3
BEAM_MAX_WIDTH = 200
SLANTED_BEAM_MIN_WIDTH = 3
CORNER_TOLERANCE = 0.1
STAFF_GAP_SPREAD = 0.4
STAFF_GAP_MIN = 2
STAFF_GAP_MAX = 9
STAFF_END_TOLERANCE = 0.3
DOT_REACH = 10
STEM_RIGHT_MIN = 3
STEM_RIGHT_MAX = 9
TUPLET_LEFT_REACH = 8
TUPLET_TICK_RISE = 3
TUPLET_RIGHT_REACH = 10
TUPLET_TICK_LEVEL = 1.5
BEAM_GROUP_MIN = 2
CLASH_REACH = 1.5
FLAG_REACH = 2.5
SYSTEM_LINE_TOLERANCE = 0.05
BAR_LINE_ALIGN = 1.5
BAR_LINE_STROKES = 3
BAR_LINE_MERGE = 5
BAR_LINE_DRIFT = 2
OCTAVE_MARK_REACH = 4
VOICE_HEAD_CLASH = 3
VOICE_STEM_CLASH = 1.5
BAR_NUMBER_REACH = 15
BASELINE_TOLERANCE = 1.5
SECOND_STAFF = 2
BRACED_PAIR = 2
TIE_LEFT_BEFORE = -9
TIE_LEFT_AFTER = 2
TIE_RIGHT_BEFORE = -7
TIE_RIGHT_AFTER = 9

NAMES = {
    "Flute I": ("Flute I",),
    "Flute II": ("Flute II",),
    "Fl.": ("Flute I", "Flute II"),
    "Oboe I": ("Oboe I",),
    "Oboe II": ("Oboe II",),
    "Ob.": ("Oboe I", "Oboe II"),
    "Clarinet I": ("Clarinet I",),
    "Clarinet II": ("Clarinet II",),
    "Cl. I": ("Clarinet I",),
    "Cl. II": ("Clarinet II",),
    "Bassoon I": ("Bassoon I",),
    "Bassoon II": ("Bassoon II",),
    "Bsn. I": ("Bassoon I",),
    "Bsn. II": ("Bassoon II",),
    "Horn I/II": ("Horn I/II",),
    "Hn. I/II": ("Horn I/II",),
    "Horn III/IV": ("Horn III/IV",),
    "Hn. III/IV": ("Horn III/IV",),
    "Timpani": ("Timpani",),
    "Timp.": ("Timpani",),
    "Harp": ("Harp",),
    "Hp.": ("Harp",),
    "Soprano": ("Soprano",),
    "S.": ("Soprano",),
    "Alto": ("Alto",),
    "A.": ("Alto",),
    "Tenor": ("Tenor",),
    "T.": ("Tenor",),
    "Bass": ("Bass",),
    "B.": ("Bass",),
    "Solo Violin": ("Solo Violin",),
    "Vln.": ("Solo Violin",),
    "Violin I": ("Violin I",),
    "Vln. I": ("Violin I",),
    "Violin II": ("Violin II",),
    "Vln. II": ("Violin II",),
    "Viola": ("Viola",),
    "Vla.": ("Viola",),
    "Violoncello": ("Violoncello",),
    "Vc.": ("Violoncello",),
    "Double Bass": ("Double Bass",),
    "Db.": ("Double Bass",),
}
TRANSPOSITIONS = {"Clarinet": Transposition(-1, -2), "Horn": Transposition(-4, -7), "Double Bass": Transposition(0, 0, -1)}


def transposition_of(name: str) -> Transposition | None:
    found = [value for prefix, value in TRANSPOSITIONS.items() if name.startswith(prefix)]
    return found[0] if found else None


def written_key(concert: int, transposition: Transposition | None) -> int:
    """Give the fifths a part prints for a concert key: written a transposition's interval above where it sounds.

    Returns:
        The written key in fifths.
    """
    if transposition is None:
        return concert
    return concert + (-transposition.chromatic * 7 + 6) % 12 - 6


@dataclasses.dataclass(frozen=True)
class Glyph:
    code: int
    x: float
    y: float
    right: float = 0.0


@dataclasses.dataclass(frozen=True)
class Staff:
    top: float
    bottom: float
    left: float
    right: float

    @property
    def middle(self) -> float:
        return (self.top + self.bottom) / 2

    @property
    def step(self) -> float:
        """Half a staff space: the distance between a line and the next space."""
        return (self.bottom - self.top) / 8


@dataclasses.dataclass
class Sheet:
    """Everything 1 page prints, by kind."""

    music: list[Glyph]
    extra: list[Glyph]
    symbols: list[Glyph]
    words: list[tuple[str, float, float, float, bool, bool]]
    lines: list[tuple[float, float, float]]
    verticals: list[tuple[float, float, float, float]]
    beams: list[tuple[float, float, float, float]]
    ticks: list[tuple[float, float, float]]
    curves: list[tuple[float, float, float, float]]
    slants: list[tuple[float, float, float, float]] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class Sound:
    """1 stem with every head on it, or 1 stemless chord."""

    staff: Staff
    x: float
    stem_x: float
    nominal: F
    dots: int
    ratio: F | None
    direction: str
    heads: list[Glyph]
    beam: int | None


@dataclasses.dataclass
class Rest:
    staff: Staff
    x: float
    y: float
    nominal: F | None
    ratio: F | None
    hidden: bool = False


def _spans(page: pymupdf.Page) -> Iterator[dict[str, Any]]:
    for block in page.get_text("rawdict")["blocks"]:
        for line in block.get("lines", []):
            yield from line["spans"]


def roles(doc: pymupdf.Document) -> dict[str, str]:
    """Find each embedded font's role.

    The music font holds the noteheads, the 3rd font the brackets, the text font the symbols; any other font prints words.

    Returns:
        Each font's role by font name.

    Raises:
        UnknownGlyph: a music font plays no known role.
    """
    music = symbolfont.font_map(doc)
    found: dict[str, str] = {}
    for page in doc:
        for span in _spans(page):
            codes = {ord(c["c"]) for c in span["chars"]}
            if span["font"] in music:
                if BLACK in codes:
                    found[span["font"]] = "music"
                elif BRACKET_TOP in codes:
                    found[span["font"]] = "extra"
            elif span["font"].split("+")[-1].startswith("TTE"):
                found.setdefault(span["font"], "symbols")
    missing = {font for font in music if font not in found}
    if missing:
        msg = f"music fonts with no known role: {sorted(missing)}"
        raise UnknownGlyph(msg)
    return found


def _read_word(span: dict[str, Any], sheet: Sheet) -> None:
    text = "".join(c["c"] for c in span["chars"])
    if text.strip():
        flags = span["flags"]
        name = span["font"].lower()
        sheet.words.append((
            text.strip(),
            span["bbox"][0],
            span["bbox"][2],
            span["origin"][1],
            bool(flags & 16) or "bold" in name,
            bool(flags & 2) or "italic" in name,
        ))


def _read_glyphs(span: dict[str, Any], kind: str, sheet: Sheet) -> None:
    for c in span["chars"]:
        code = ord(c["c"])
        if c["c"].isspace():
            continue
        if code not in symbolfont.KNOWN_GLYPHS or code not in FONT_MEANINGS[kind]:
            msg = f"music glyph {code:#x} of the {kind} font is not decoded"
            raise UnknownGlyph(msg)
        getattr(sheet, kind).append(Glyph(code, c["origin"][0], c["origin"][1], c["bbox"][2]))


def _read_stroke(d: dict[str, Any], r: pymupdf.Rect, sheet: Sheet) -> None:
    a, b = d["items"][0][1], d["items"][0][2]
    if abs(a.y - b.y) < STRAIGHT_TOLERANCE and r.width > STAFF_LINE_MIN:
        sheet.lines.append((a.y, r.x0, r.x1))
    elif abs(a.y - b.y) < 1 and TICK_MIN_WIDTH < r.width <= STAFF_LINE_MIN:
        sheet.ticks.append(((a.y + b.y) / 2, r.x0, r.x1))
    elif abs(a.x - b.x) < STRAIGHT_TOLERANCE:
        sheet.verticals.append((a.x, r.y0, r.y1, d["width"] or 0))
    elif HAIRPIN_RISE[0] < abs(a.y - b.y) < HAIRPIN_RISE[1] and abs(a.x - b.x) > HAIRPIN_MIN:
        left, right = (a, b) if a.x < b.x else (b, a)
        sheet.slants.append((left.x, left.y, right.x, right.y))


def _corner_mean(corners: list[Any], at: float) -> float:
    near = [p.y for p in corners if abs(p.x - at) < CORNER_TOLERANCE]
    return float(sum(near)) / max(1, len(near))


def _read_drawing(d: dict[str, Any], page_width: float, sheet: Sheet) -> None:
    kinds = "".join(i[0] for i in d["items"])
    r = d["rect"]
    if kinds == "l":
        _read_stroke(d, r, sheet)
    elif (
        kinds == "re"
        and d["type"] == "f"
        and r.height < BEAM_MAX_HEIGHT
        and BEAM_MIN_WIDTH < r.width < BEAM_MAX_WIDTH
    ):
        sheet.beams.append((r.x0, r.x1, (r.y0 + r.y1) / 2, (r.y0 + r.y1) / 2))
    elif kinds == "llll" and SLANTED_BEAM_MIN_WIDTH < r.width < page_width - 1:
        corners = [p for item in d["items"] for p in (item[1], item[2])]
        lo, hi = min(p.x for p in corners), max(p.x for p in corners)
        sheet.beams.append((lo, hi, _corner_mean(corners, lo), _corner_mean(corners, hi)))
    elif kinds == "cc" and d["type"] == "f":
        start, end = d["items"][0][1], d["items"][0][4]
        sheet.curves.append((start.x, start.y, end.x, end.y))


def read(page: pymupdf.Page, role: dict[str, str]) -> Sheet:
    sheet = Sheet([], [], [], [], [], [], [], [], [])
    for span in _spans(page):
        kind = role.get(span["font"])
        if kind is None:
            _read_word(span, sheet)
            continue
        _read_glyphs(span, kind, sheet)
    for d in page.get_drawings():
        _read_drawing(d, page.rect.width, sheet)
    return sheet


def hairpins(slants: list[tuple[float, float, float, float]]) -> list[tuple[str, float, float, float]]:
    """Find every hairpin: 2 straight strokes meeting at 1 end and opening at the other.

    A crescendo is a hairpin whose strokes meet on the left.

    Returns:
        Each hairpin's kind, left, right and the height of its point.
    """
    found = []
    used: set[int] = set()
    for i, one in enumerate(slants):
        for j in range(i + 1, len(slants)):
            if i in used or j in used:
                continue
            two = slants[j]
            if (
                abs(one[0] - two[0]) < HAIRPIN_JOIN
                and abs(one[1] - two[1]) < HAIRPIN_JOIN
                and abs(one[2] - two[2]) < 2 * HAIRPIN_JOIN
                and abs(one[3] - two[3]) > HAIRPIN_OPEN
            ):
                found.append(("crescendo", one[0], one[2], one[1]))
                used |= {i, j}
            elif (
                abs(one[2] - two[2]) < HAIRPIN_JOIN
                and abs(one[3] - two[3]) < HAIRPIN_JOIN
                and abs(one[0] - two[0]) < 2 * HAIRPIN_JOIN
                and abs(one[1] - two[1]) > HAIRPIN_OPEN
            ):
                found.append(("diminuendo", one[0], one[2], one[3]))
                used |= {i, j}
    return found


def staves(lines: list[tuple[float, float, float]]) -> list[Staff]:
    """Find every 5 equally spaced staff lines, top down.

    Returns:
        The staves, top down.
    """
    ys = sorted({round(y, 1) for y, _, _ in lines})
    out: list[Staff] = []
    at = 0
    while at + 4 < len(ys):
        gaps = [ys[at + i + 1] - ys[at + i] for i in range(4)]
        if max(gaps) - min(gaps) < STAFF_GAP_SPREAD and STAFF_GAP_MIN < min(gaps) < STAFF_GAP_MAX:
            top, bottom = ys[at], ys[at + 4]
            ends = [(a, b) for y, a, b in lines if abs(y - top) < STAFF_END_TOLERANCE]
            out.append(Staff(top, bottom, min(a for a, _ in ends), max(b for _, b in ends)))
            at += 5
        else:
            at += 1
    return out


def dot_count(heads: list[Glyph], dots: list[Glyph], step: float) -> int:
    """Count the augmentation dots right of a chord: 1 dot makes 3/2, 2 dots 7/4.

    Returns:
        The number of dots.
    """
    return max(
        len({round(f.x) for f in dots if 0 < f.x - h.x < DOT_REACH and abs(f.y - h.y) < step * 2}) for h in heads
    )


def nearest_of(found: list[Staff]) -> Callable[[float], Staff]:
    return lambda y: min(found, key=lambda s: abs(s.middle - y))


type _Stem = tuple[float, float, float, float]
type _Beam = tuple[float, float, float, float]


@dataclasses.dataclass(frozen=True)
class _Layout:
    found: list[Staff]
    braces: list[tuple[float, float]]
    nearest: Callable[[float], Staff]
    step: float
    heads: list[Glyph]
    dots: list[Glyph]
    flags: list[Glyph]


@dataclasses.dataclass
class _Stems:
    heads: dict[_Stem, list[Glyph]]
    touching: dict[_Stem, list[_Beam]]
    group: dict[_Stem, _Stem]
    scale: dict[Any, F] = dataclasses.field(default_factory=dict)
    home: dict[_Stem, Staff] = dataclasses.field(default_factory=dict)

    def root(self, st: _Stem) -> _Stem:
        while self.group[st] != st:
            st = self.group[st]
        return st


def _attach_heads(
    heads: list[Glyph],
    stems: list[tuple[float, float, float, float]],
) -> tuple[dict[_Stem, list[Glyph]], list[Glyph]]:
    on_stem: dict[_Stem, list[Glyph]] = collections.defaultdict(list)
    loose = []
    for g in heads:
        near = (
            [
                s
                for s in stems
                if (-1 <= s[0] - g.x <= 1 or STEM_RIGHT_MIN <= s[0] - g.x <= STEM_RIGHT_MAX)
                and s[1] - 1 <= g.y <= s[2] + 1
            ]
            if g.code not in {WHOLE, DIAMOND_WHOLE}
            else []
        )
        if near:
            on_stem[min(near, key=lambda s: abs(s[0] - g.x))].append(g)
        else:
            loose.append(g)
    return on_stem, loose


def _beam_groups(on_stem: dict[_Stem, list[Glyph]], touching: dict[_Stem, list[_Beam]], beams: list[_Beam]) -> _Stems:
    stems = _Stems(on_stem, touching, {st: st for st in on_stem})
    for b in beams:
        members = [st for st in on_stem if b in touching[st]]
        for st in members[1:]:
            stems.group[stems.root(st)] = stems.root(members[0])
    return stems


def _bracket_ticks(
    ticks: list[tuple[float, float, float]],
    x: float,
    y: float,
) -> tuple[list[tuple[float, float, float]], list[tuple[float, float, float]]]:
    left = [t for t in ticks if t[2] < x and x - t[2] < TUPLET_LEFT_REACH and abs(t[0] - (y - 2.5)) < TUPLET_TICK_RISE]
    right = [
        t
        for t in ticks
        if t[1] > x
        and t[1] - x < TUPLET_RIGHT_REACH
        and left
        and any(abs(t[0] - lt[0]) < TUPLET_TICK_LEVEL for lt in left)
    ]
    return left, right


def _stems_under(stems: _Stems, span: tuple[Staff, float, float, F], level: float, layout: _Layout) -> list[_Stem]:
    nearest = layout.nearest
    return [
        st
        for st, hs in stems.heads.items()
        if span[1] <= st[0] <= span[2]
        and min(abs(st[1] - level), abs(st[2] - level)) < 12 * layout.step
        and any(nearest(h.y) == nearest(level) for h in hs)
    ]


def _nearest_beam(stems: _Stems, x: float, y: float, step: float) -> tuple[float, Any] | None:
    roots = collections.defaultdict(list)
    for st in stems.heads:
        roots[stems.root(st)].append(st)
    best: tuple[float, Any] | None = None
    for root, members in roots.items():
        lo, hi = min(s[0] for s in members) - 4, max(s[0] for s in members) + 4
        if len(members) < BEAM_GROUP_MIN or not lo <= x <= hi:
            continue
        gap = min(abs(y - e) for s in members for e in (s[1], s[2]))
        if gap < 6 * step and (best is None or gap < best[0]):
            best = (gap, root)
    return best


def _tuplet_scales(sheet: Sheet, stems: _Stems, layout: _Layout) -> list[tuple[Staff, float, float, F]]:
    spans: list[tuple[Staff, float, float, F]] = []
    for digit in sheet.symbols:
        if digit.code not in TUPLET:
            continue
        ratio, x, y = TUPLET[digit.code], digit.x, digit.y
        left, right = _bracket_ticks(sheet.ticks, x, y)
        if left and right:
            lt = max(left, key=operator.itemgetter(2))
            rt = min((t for t in right if abs(t[0] - lt[0]) < TUPLET_TICK_LEVEL), key=operator.itemgetter(1))
            span = (layout.nearest(lt[0]), lt[1] - 1.5, rt[2] + 1.5, ratio)
            spans.append(span)
            for st in _stems_under(stems, span, lt[0], layout):
                stems.scale["stem", st] = ratio
            continue
        best = _nearest_beam(stems, x, y, layout.step)
        if best:
            stems.scale[best[1]] = ratio
    return spans


def _home_of(members: list[_Stem], stems: _Stems, layout: _Layout) -> Staff:
    nearest = layout.nearest
    held = {nearest(h.y) for st in members for h in stems.heads[st]} | {
        s for st in members for s in layout.found if st[1] <= s.bottom and st[2] >= s.top
    }
    held |= {
        s
        for s in layout.found
        for b0, b1 in layout.braces
        for h in list(held)
        if b0 <= h.middle <= b1 and b0 <= s.middle <= b1
    }
    at = sum(beam_y(stems.touching[st][0], st[0]) for st in members) / len(members)
    mine = [h for st in members for h in stems.heads[st]]

    def clash(s: Staff, mine: list[Glyph] = mine) -> int:
        return sum(
            1
            for h in layout.heads
            if h not in mine and nearest(h.y) == s and any(abs(h.x - m.x) < CLASH_REACH for m in mine)
        )

    between = any(a.bottom < at < b.top for a in held for b in held)
    return min(held, key=lambda s: (clash(s) if between else 0, min(abs(at - s.top), abs(at - s.bottom))))


def _beam_homes(stems: _Stems, layout: _Layout) -> dict[_Stem, Staff]:
    home = {}
    for root in {stems.root(st) for st in stems.heads}:
        members = [st for st in stems.heads if stems.root(st) == root and stems.touching[st]]
        if not members:
            continue
        home[root] = _home_of(members, stems, layout)
    return home


def _stem_sound(st: _Stem, hs: list[Glyph], stems: _Stems, layout: _Layout) -> Sound:
    nearest = layout.nearest
    hs.sort(key=lambda h: h.y)
    middle = sum(h.y for h in hs) / len(hs)
    up = middle - st[1] > st[2] - middle
    level = max(
        len(stems.touching[st]),
        max((1 for f in layout.flags if abs(f.x - st[0]) < FLAG_REACH and st[1] - 6 <= f.y <= st[2] + 6), default=0),
    )
    value = HEADS[hs[0].code]
    if hs[0].code == BLACK:
        value /= 2**level
    count = dot_count(hs, layout.dots, layout.step)
    scaled = stems.scale.get(("stem", st), stems.scale.get(stems.root(st)))
    staff = (
        stems.home[stems.root(st)]
        if stems.touching[st]
        else collections.Counter(nearest(h.y) for h in hs).most_common(1)[0][0]
    )
    return Sound(
        staff,
        hs[0].x,
        st[0],
        value * (2 - F(1, 2**count)),
        count,
        scaled,
        "u" if up else "d",
        hs,
        id(stems.root(st)) if stems.touching[st] else None,
    )


def _loose_chords(loose: list[Glyph], layout: _Layout) -> list[Sound]:
    chords: dict[tuple[Staff, int], Sound] = {}
    for g in loose:
        key = (layout.nearest(g.y), round(g.x))
        if key in chords:
            chords[key].heads.append(g)
            continue
        count = dot_count([g], layout.dots, layout.step)
        chords[key] = Sound(key[0], g.x, g.x, HEADS[g.code] * (2 - F(1, 2**count)), count, None, "n", [g], None)
    return list(chords.values())


def sounds(
    sheet: Sheet,
    found: list[Staff],
    braces: list[tuple[float, float]],
) -> tuple[list[Sound], list[tuple[Staff, float, float, F]]]:
    """Find every sounding event of the page, and the bracketed tuplets' spans: staff, left, right, ratio.

    Returns:
        The page's sounds, and each bracketed tuplet's staff, left, right and ratio.
    """
    nearest = nearest_of(found)
    step = found[0].step
    dots = [g for g in sheet.extra if g.code == AUGMENTATION_DOT]
    flags = [g for g in sheet.music if g.code in {UP_FLAG, DOWN_FLAG}]
    stem_lines = [v for v in sheet.verticals if v[3] < STEM_WIDTH]
    heads = [g for g in sheet.music + sheet.extra if g.code in HEADS]
    on_stem, loose = _attach_heads(heads, stem_lines)
    touching = {
        st: [b for b in sheet.beams if b[0] - 1 <= st[0] <= b[1] + 1 and st[1] - 2 <= beam_y(b, st[0]) <= st[2] + 2]
        for st in on_stem
    }
    stems = _beam_groups(on_stem, touching, sheet.beams)
    layout = _Layout(found, braces, nearest, step, heads, dots, flags)
    spans = _tuplet_scales(sheet, stems, layout)
    stems.home = _beam_homes(stems, layout)
    out = [_stem_sound(st, hs, stems, layout) for st, hs in on_stem.items()]
    return out + _loose_chords(loose, layout), spans


@dataclasses.dataclass
class System:
    page: int
    staves: list[Staff]
    names: list[str]
    bars: list[float]
    above: float = float("-inf")
    below: float = float("inf")

    def holds(self, y: float, reach_above: float, reach_below: float) -> bool:
        """Tell whether a word or mark at height y is this system's.

        It is when within its reach of the staves, and on its own side of the halfway line to each neighbour.

        Returns:
            Whether the system holds the height.
        """
        return (
            max(self.staves[0].top - reach_above, self.above) < y < min(self.staves[-1].bottom + reach_below, self.below)
        )


def systems_of(page_no: int, sheet: Sheet, found: list[Staff], braces: list[tuple[float, float]]) -> list[System]:
    """Find the page's systems, each joined at its left by 1 thin line, every staff named from its own label.

    Returns:
        The page's systems, top down.

    Raises:
        Unreadable: a staff belongs to no system.
    """
    left = min(s.left for s in found)
    joins = sorted({
        (round(v[1], 1), round(v[2], 1))
        for v in sheet.verticals
        if abs(v[0] - left) < 1 and abs(v[3] - SYSTEM_LINE) < SYSTEM_LINE_TOLERANCE
    })
    out = []
    for y0, y1 in joins:
        members = [s for s in found if y0 - 1 <= s.top and s.bottom <= y1 + 1]
        if members:
            out.append(System(page_no, members, names_of(sheet, members, braces, page_no), []))
    alone = [s for s in found if not any(s in system.staves for system in out)]
    if alone:
        msg = f"page {page_no + 1}: staves at {[round(s.top) for s in alone]} belong to no system"
        raise Unreadable(msg)
    return out


def names_of(sheet: Sheet, members: list[Staff], braces: list[tuple[float, float]], page_no: int) -> list[str]:
    """Each staff's part from the label printed left of it; a label inside a brace names both staves the brace joins.

    Returns:
        Each staff's part name, top down.

    Raises:
        Unreadable: a staff prints no label, an unknown label, or a shared label that names no part for certain.
    """
    labels = [
        (" ".join(text.split()), y)
        for text, _, right, y, _, _ in sheet.words
        if right < members[0].left and not text.isdigit() and members[0].top - 20 < y < members[-1].bottom + 20
    ]
    printed: list[str | None] = []
    for staff in members:
        brace = next(((b0, b1) for b0, b1 in braces if b0 <= staff.middle <= b1), None)
        if brace:
            inside = [text for text, y in labels if brace[0] <= y <= brace[1]]
            printed.append(inside[0] if len(inside) == 1 else None)
        else:
            near = [text for text, y in labels if abs(y - staff.middle) < staff.step * LABEL_REACH]
            printed.append(near[0] if len(near) == 1 else None)
    names: list[str] = []
    for staff, label in zip(members, printed, strict=True):
        if label is None:
            msg = f"page {page_no + 1}: the staff at {staff.top:.0f} prints no label of its own"
            raise Unreadable(msg)
        if label not in NAMES:
            msg = f"page {page_no + 1}: unknown staff label {label!r}"
            raise Unreadable(msg)
        brace = next(((b0, b1) for b0, b1 in braces if b0 <= staff.middle <= b1), None)
        if brace and names and names[-1] in NAMES[label] and printed[len(names) - 1] == label:
            names.append(names[-1])
            continue
        sharing = sum(1 for other in printed if other == label)
        options = NAMES[label]
        if len(options) > 1 and sharing != len(options):
            msg = (
                f"page {page_no + 1}: label {label!r} names {len(options)} parts but prints on {sharing} staves,"
                " so which one it names is unknown"
            )
            raise Unreadable(
                msg,
            )
        names.append(
            options[sum(1 for earlier in printed[: len(names)] if earlier == label)] if len(options) > 1 else options[0],
        )
    return names


def bar_lines(staff: Staff, verticals: list[tuple[float, float, float, float]]) -> list[float]:
    """Find the bar lines crossing 1 staff: strokes as wide as a bar line that span it, and its right end.

    Returns:
        The bar lines' x, left to right.
    """
    rules = [v for v in verticals if v[3] >= STEM_WIDTH]
    spans = [round(x, 0) for x, y0, y1, _ in rules if y0 - 1 <= staff.top and y1 + 1 >= staff.bottom]
    height = staff.bottom - staff.top
    bars = sorted({
        x
        for x in spans
        if sum(1 for other, o0, o1, _ in rules if abs(other - x) < BAR_LINE_ALIGN and o1 - o0 > height - 1)
        >= BAR_LINE_STROKES
        or any(abs(other - x) < BAR_LINE_ALIGN and o1 - o0 > 3 * height for other, o0, o1, _ in rules)
    })
    merged: list[float] = []
    for x in bars:
        if not merged or x - merged[-1] > BAR_LINE_MERGE:
            merged.append(x)
    if merged and merged[-1] < staff.right - 5:
        merged.append(staff.right)
    return merged


def system_bars(system: System, verticals: list[tuple[float, float, float, float]]) -> list[float]:
    """Find the bar lines every staff of a system shares; a staff that disagrees is refused.

    Returns:
        The shared bar lines' x, left to right.

    Raises:
        Unreadable: a staff's bar lines disagree with its system's.
    """
    each = [bar_lines(staff, verticals) for staff in system.staves]
    common = max(each, key=len)
    for staff, found in zip(system.staves, each, strict=True):
        if len(found) != len(common) or any(abs(a - b) > BAR_LINE_DRIFT for a, b in zip(found, common, strict=True)):
            msg = f"page {system.page + 1}: the staff at {staff.top:.0f} has bar lines {found}, its system {common}"
            raise Unreadable(
                msg,
            )
    return common


def clef_of(glyph: Glyph, staff: Staff, extra: list[Glyph]) -> tuple[Clef, int]:
    """Read a clef's sign, line and octave, and the diatonic index of its staff's bottom line.

    Returns:
        The clef, and the diatonic index of its staff's bottom line.
    """
    sign, origin = CLEF_LINES[glyph.code]
    line = 1 + round((staff.bottom - glyph.y) / (2 * staff.step))
    octave = (
        -1
        if glyph.code == TREBLE_CLEF
        and any(
            g.code == OCTAVE_BELOW and abs(g.x - glyph.x) < OCTAVE_MARK_REACH and 0 < g.y - glyph.y < staff.step * 12
            for g in extra
        )
        else 0
    )
    return (sign, line, octave), origin - 2 * (line - 1) + 7 * octave


def pitch_of(y: float, staff: Staff, bottom_index: int) -> tuple[str, int]:
    index = bottom_index + round((staff.bottom - y) / staff.step)
    return LETTERS[index % 7], index // 7


DEFAULT_ORDER = "FCGDAEB"


def key_alter(step: str, key: int) -> int:
    if key > 0 and DEFAULT_ORDER.index(step) < key:
        return 1
    if key < 0 and DEFAULT_ORDER[::-1].index(step) < -key:
        return -1
    return 0


@dataclasses.dataclass
class Reading:
    """What every staff of the score holds, gathered system by system."""

    parts: dict[str, list[Bar]] = dataclasses.field(default_factory=dict)
    staves: dict[str, int] = dataclasses.field(default_factory=dict)
    keys: dict[str, int] = dataclasses.field(default_factory=dict)
    abbreviations: dict[str, str] = dataclasses.field(default_factory=dict)
    order: list[str] = dataclasses.field(default_factory=list)
    clefs: dict[tuple[str, int], Clef] = dataclasses.field(default_factory=dict)
    frames: list[tuple[int, bool, bool]] = dataclasses.field(default_factory=list)
    time: tuple[int, int] | None = None


def _voice_split(sounds_here: list[Sound]) -> bool:
    def at(e: Sound) -> tuple[float, float]:
        return e.x, e.stem_x

    def close(p: tuple[float, float], q: tuple[float, float]) -> bool:
        return abs(p[0] - q[0]) < VOICE_HEAD_CLASH or abs(p[1] - q[1]) < VOICE_STEM_CLASH

    ups, downs, stemless = ([at(e) for e in sounds_here if e.direction == k] for k in "udn")
    return any(close(u, d) for u in ups for d in downs) or any(close(n, m) for n in stemless for m in ups + downs)


def _filled(members: list[Sound | Rest], length: F) -> F:
    return sum((rest_length(m, length) if isinstance(m, Rest) else m.nominal * (m.ratio or 1) for m in members), F(0))


def _fill_voices(
    voice: dict[str, list[Sound | Rest]],
    keys: list[str],
    rests: list[Rest],
    length: F,
) -> list[list[Sound | Rest]] | None:
    for choice in sorted(_assignments(len(rests), len(keys)), key=lambda c: sum(v == len(keys) for v in c)):
        trial = {k: list(voice[k]) for k in keys}
        for rest, at_voice in zip(rests, choice, strict=True):
            if at_voice < len(keys):
                trial[keys[at_voice]].append(rest)
            else:
                for order, k in enumerate(keys):
                    trial[k].append(rest if not order else dataclasses.replace(rest, hidden=True))
        if all(_filled(trial[k], length) == length for k in keys):
            return [sorted(trial[k], key=lambda m: m.x) for k in keys]
    return None


def voices_of(sounds_here: list[Sound], rests: list[Rest], length: F, where: str) -> list[list[Sound | Rest]]:
    """Part a staff-bar's voices; each voice and its rests fill the bar.

    Voices part only where an up and a down stem, or a stemless chord and a stem, stand at 1 x.

    Returns:
        The bar's voices, each in x order.

    Raises:
        Unreadable: no way to give the rests to the voices fills every voice.
    """
    split = _voice_split(sounds_here)
    voice: dict[str, list[Sound | Rest]] = collections.defaultdict(list)
    for e in sounds_here:
        voice[e.direction if split or e.direction == "n" else "s"].append(e)
    if not split and "n" in voice and "s" in voice:
        voice["s"] += voice.pop("n")
    if not voice:
        voice["s"] = []
    keys = sorted(voice, key="usnd".index)
    if not sounds_here and not rests:
        return [[]]
    best = _fill_voices(voice, keys, rests, length)
    if best is None:
        raise Unreadable(
            f"{where}: no voice adds up to {length} quarters: "
            + "; ".join(f"{k} {_filled(voice[k], length)}" for k in keys)
            + f"; rests {[str(rest_length(r, length)) for r in rests]}",
        )
    return best


def rest_length(rest: Rest, length: F) -> F:
    return length if rest.nominal is None else rest.nominal * (rest.ratio or 1)


def _assignments(count: int, voices: int) -> list[tuple[int, ...]]:
    """Give each rest to 1 voice, or to every voice at once, every way.

    Returns:
        Each assignment: per rest, its voice, or the voice count for every voice.
    """
    if voices == 1:
        return [tuple([0] * count)]
    out: list[tuple[int, ...]] = [()]
    for _ in range(count):
        out = [(*prefix, v) for prefix in out for v in range(voices + 1)]
    return out


@dataclasses.dataclass(frozen=True)
class _PageItems:
    sheet: Sheet
    sounds: list[Sound]
    rests: list[Rest]


@dataclasses.dataclass(frozen=True)
class _SystemFrame:
    system: System
    page: _PageItems
    nearest: Callable[[float], Staff]
    edges: list[tuple[float, float]]
    first: int
    length: F


def decode(path: str | pathlib.Path) -> Orchestra:
    doc = pymupdf.open(path)
    role = roles(doc)
    reading = Reading()
    for page_no in range(doc.page_count):
        page = doc.load_page(page_no)
        sheet = read(page, role)
        found = staves(sheet.lines)
        if not found:
            continue
        braces = brace_spans(page, role)
        page_sounds, spans = sounds(sheet, found, braces)
        rests = rests_of(sheet, found, spans)
        page_systems = systems_of(page_no, sheet, found, braces)
        for upper, lower in itertools.pairwise(page_systems):
            upper.below = lower.above = (upper.staves[-1].bottom + lower.staves[0].top) / 2
        items = _PageItems(sheet, page_sounds, rests)
        for at, system in enumerate(page_systems):
            system.bars = system_bars(system, sheet.verticals)
            read_system(reading, system, items, opens_page=at == 0)
    return finish(reading)


def brace_spans(page: pymupdf.Page, role: dict[str, str]) -> list[tuple[float, float]]:
    """Measure the height each brace covers, from its glyph's own box.

    Returns:
        Each brace's top and bottom.
    """
    out: list[tuple[float, float]] = []
    for span in _spans(page):
        if role.get(span["font"]) == "extra":
            out += [(c["bbox"][1], c["bbox"][3]) for c in span["chars"] if ord(c["c"]) == BRACE]
    return out


def rests_of(sheet: Sheet, found: list[Staff], spans: list[tuple[Staff, float, float, F]]) -> list[Rest]:
    nearest = nearest_of(found)
    out = []
    for g in sheet.music:
        if g.code in RESTS:
            staff = nearest(g.y)
            ratio = next((r for s, x0, x1, r in spans if s == staff and x0 <= g.x <= x1), None)
            out.append(Rest(staff, g.x, g.y, RESTS[g.code], ratio))
    return out


CLEF_REACH = 4


def clefs_on(sheet: Sheet, staff: Staff, nearest: Callable[[float], Staff]) -> list[Glyph]:
    """Find the clefs standing on 1 staff.

    A clef stands on the staff within its own lines and a ledger's reach, and nearer it than any other staff of its system.

    Returns:
        The staff's clefs, left to right.
    """
    reach = staff.step * CLEF_REACH
    return sorted(
        (
            g
            for g in sheet.music
            if g.code in CLEF_LINES and staff.top - reach <= g.y <= staff.bottom + reach and nearest(g.y) == staff
        ),
        key=lambda g: g.x,
    )


def read_system(reading: Reading, system: System, page: _PageItems, *, opens_page: bool) -> None:
    """Every staff of 1 system into its part's bars; a part the system hides rests through it.

    Raises:
        Unreadable: the system's bar number disagrees with the reading, or no time signature precedes its first bar.
    """
    sheet = page.sheet
    edges = list(zip(system.bars, system.bars[1:], strict=False))
    first = len(reading.frames) + 1
    where = f"page {system.page + 1}"
    printed = [
        int(text)
        for text, _, right, y, _, _ in sheet.words
        if text.isdigit() and right < system.staves[0].left and abs(y - system.staves[0].top) < BAR_NUMBER_REACH
    ]
    if printed and printed[0] != first:
        msg = f"{where}: the system prints bar {printed[0]}, the reading reaches bar {first}"
        raise Unreadable(msg)
    if any(g.code == TIME_FOUR and system.staves[0].top - 1 <= g.y <= system.staves[-1].bottom + 1 for g in sheet.music):
        reading.time = (4, 4)
    if reading.time is None:
        msg = f"{where}: no time signature before the first bar"
        raise Unreadable(msg)
    length = F(reading.time[0] * 4, reading.time[1])
    nearest = nearest_of(system.staves)
    reading.frames += [
        (system.page, at == 0 and first > 1, at == 0 and first > 1 and opens_page) for at in range(len(edges))
    ]
    frame = _SystemFrame(system, page, nearest, edges, first, length)
    names = list(dict.fromkeys(system.names))
    for name in names:
        own = [staff for staff, other in zip(system.staves, system.names, strict=True) if other == name]
        if name not in reading.parts:
            reading.order.append(name)
            reading.parts[name] = [empty_bar(n, length) for n in range(1, first)]
        reading.staves[name] = max(reading.staves.get(name, 1), len(own))
        label = next(
            " ".join(text.split())
            for text, _, right, y, _, _ in sheet.words
            if right < own[0].left
            and " ".join(text.split()) in NAMES
            and name in NAMES[" ".join(text.split())]
            and own[0].top - 20 < y < own[-1].bottom + 20
        )
        if label != name:
            reading.abbreviations.setdefault(name, label)
        read_part(reading, name, own, frame)
    for name, bars in reading.parts.items():
        if name not in names:
            bars += [empty_bar(first + at, length) for at in range(len(edges))]
    marks_into(reading, system, sheet, first, edges)


def owner(system: System, y: float, *, below: bool) -> tuple[str, int]:
    """Find the part and staff a mark belongs to: a text prints above its staff, a dynamic below it.

    Returns:
        The part's name and the staff's number within it.
    """
    staves = system.staves
    if below:
        above = [at for at, s in enumerate(staves) if s.top < y]
        at = above[-1] if above else 0
    else:
        under = [at for at, s in enumerate(staves) if s.bottom > y]
        at = under[0] if under else len(staves) - 1
    name = system.names[at]
    return name, 2 if at and system.names[at - 1] == name else 1


def phrases(
    pieces: list[tuple[str, float, float, float, bool, bool]],
) -> list[tuple[str, float, float, float, bool, bool]]:
    """Pieces on 1 baseline, each close after the last, joined into 1 phrase.

    Returns:
        The phrases, row by row, left to right.
    """
    rows: list[list[tuple[str, float, float, float, bool, bool]]] = []
    for piece in sorted(pieces, key=operator.itemgetter(3)):
        if rows and abs(rows[-1][0][3] - piece[3]) < BASELINE_TOLERANCE:
            rows[-1].append(piece)
        else:
            rows.append([piece])
    out: list[tuple[str, float, float, float, bool, bool]] = []
    for row in rows:
        joined: list[tuple[str, float, float, float, bool, bool]] = []
        for piece in sorted(row, key=operator.itemgetter(1)):
            gap = piece[1] - joined[-1][2] if joined else None
            if gap is not None and -1 < gap < 2 * WORD_GAP:
                last = joined[-1]
                joined[-1] = (
                    last[0] + ("" if gap < WORD_GAP else " ") + piece[0],
                    last[1],
                    piece[2],
                    last[3],
                    last[4],
                    last[5],
                )
            else:
                joined.append(piece)
        out += joined
    return out


def _texts_into(system: System, sheet: Sheet, bar_of: Callable[[str, float], Bar]) -> None:
    top = system.staves[0].top
    left = system.staves[0].left
    inside = [
        w
        for w in sheet.words
        if w[1] >= left and system.holds(w[3], HEADER_REACH, MARK_REACH_BELOW) and not (w[0].isdigit() and w[3] < top)
    ]
    letters = [
        (DYNAMIC_LETTERS[g.code], g.x, g.right, g.y, False, True)
        for g in sheet.symbols
        if g.code in DYNAMIC_LETTERS and system.holds(g.y, MARK_REACH_ABOVE, MARK_REACH_BELOW)
    ]
    signs = [
        (TEXT_SPELLING[g.code], g.x, g.right, g.y, False, False)
        for g in sheet.symbols
        if g.code in TEXT_SPELLING and system.holds(g.y, MARK_REACH_ABOVE, MARK_REACH_BELOW)
    ]
    for text, x0, _, y, bold, italic in phrases(inside + signs):
        name, number = owner(system, y, below=False)
        bar = bar_of(name, x0)
        (bar.texts if number == 1 else bar.texts2).append(Text(x0, y, [(text, bold, italic)], above=True))
    for text, x0, _, y, _, _ in phrases(letters):
        name, number = owner(system, y, below=True)
        bar = bar_of(name, x0)
        (bar.texts if number == 1 else bar.texts2).append(Text(x0, y, [(text, False, True)], above=False, kind="dynamic"))


def _hairpins_into(system: System, sheet: Sheet, bar_of: Callable[[str, float], Bar]) -> None:
    for kind, x0, x1, y in hairpins(sheet.slants):
        if not system.holds(y, MARK_REACH_ABOVE, MARK_REACH_BELOW) or not system.bars[0] <= x0 < system.bars[-1]:
            continue
        name, number = owner(system, y, below=True)
        start, stop = bar_of(name, x0), bar_of(name, min(x1, system.bars[-1] - 0.5))
        (start.texts if number == 1 else start.texts2).append(Text(x0, y, [("", False, False)], above=False, kind=kind))
        (stop.texts if number == 1 else stop.texts2).append(
            Text(x1, y, [("", False, False)], above=False, kind="wedge-stop"),
        )


def _note_mark(system: System, g: Glyph, mark: str, bar_of: Callable[[str, float], Bar]) -> None:
    reach = ARPEGGIO_REACH if mark == "arpeggiate" else ARTICULATION_REACH + 3
    near: list[Event] = []
    for at in sorted(
        range(len(system.staves)),
        key=lambda i: max(system.staves[i].top - g.y, g.y - system.staves[i].bottom, 0.0),
    ):
        name = system.names[at]
        bar = bar_of(name, g.x)
        own = bar.voices2 if at and system.names[at - 1] == name else bar.voices
        near = [
            e
            for voice in own
            for e in voice
            if e.heads and (0 < e.x - g.x < reach if mark == "arpeggiate" else abs(e.x - g.x) < reach)
        ]
        if near:
            break
    if not near:
        msg = f"page {system.page + 1}: {mark} at {g.x:.0f} stands by no note"
        raise Unreadable(msg)
    chosen = min(near, key=lambda e: (round(abs(e.x - g.x)), min(abs(h.y - g.y) for h in e.heads)))
    if mark not in chosen.marks:
        chosen.marks.append(mark)


def _flags_into(system: System, sheet: Sheet, bar_of: Callable[[str, float], Bar]) -> None:
    for g in sheet.music:
        flag = {ACCENT_BELOW: "accent", FERMATA: "fermata", TRILL: "trill"}.get(g.code)
        if flag is None or not system.holds(g.y, MARK_REACH_ABOVE, MARK_REACH_BELOW):
            continue
        name, _ = owner(system, g.y, below=g.code == ACCENT_BELOW)
        bar = bar_of(name, g.x)
        events = [
            e
            for voices in (bar.voices, bar.voices2)
            for voice in voices
            for e in voice
            if e.heads and abs(e.x - g.x) < ARTICULATION_REACH + 3
        ]
        if not events:
            msg = f"page {system.page + 1}: {flag} at {g.x:.0f} stands by no note of {name}"
            raise Unreadable(msg)
        setattr(min(events, key=lambda e: abs(e.x - g.x)), flag, True)


def marks_into(reading: Reading, system: System, sheet: Sheet, first: int, edges: list[tuple[float, float]]) -> None:
    """Place the system's words, dynamics and articulations, each in the bar of the part it prints by."""

    def bar_of(name: str, x: float) -> Bar:
        at = next((i for i, (lo, hi) in enumerate(edges) if x < hi), len(edges) - 1)
        return reading.parts[name][first - 1 + at]

    _texts_into(system, sheet, bar_of)
    _hairpins_into(system, sheet, bar_of)
    for g in [*sheet.music, *sheet.extra]:
        mark = NOTE_MARKS.get(g.code)
        if (
            mark is None
            or not system.holds(g.y, MARK_REACH_ABOVE, MARK_REACH_BELOW)
            or not system.bars[0] <= g.x < system.bars[-1]
        ):
            continue
        _note_mark(system, g, mark, bar_of)
    _flags_into(system, sheet, bar_of)


@dataclasses.dataclass(frozen=True)
class _Part:
    name: str
    own: list[Staff]
    frame: _SystemFrame
    opening: dict[Staff, Glyph]
    changes: dict[Staff, list[Glyph]]
    sounds: list[Sound]
    rests: list[Rest]
    key: int
    accidentals: list[Glyph]

    def clef_at(self, staff: Staff, x: float) -> Glyph:
        return ([g for g in self.changes[staff] if g.x < x] or [self.opening[staff]])[-1]


@dataclasses.dataclass(frozen=True)
class _Window:
    lo: float
    hi: float
    length: F
    where: str


@dataclasses.dataclass(frozen=True)
class _Pitching:
    own: list[Staff]
    nearest: Callable[[float], Staff]
    clef_at: Callable[[Staff, float], Glyph]
    extra: list[Glyph]
    accidentals: list[Glyph]
    key: int
    state: dict[tuple[int, str, int], int]
    number: int | None
    length: F


def _opening_clefs(
    name: str,
    own: list[Staff],
    frame: _SystemFrame,
) -> tuple[dict[Staff, Glyph], dict[Staff, list[Glyph]]]:
    where = f"page {frame.system.page + 1}"
    opening: dict[Staff, Glyph] = {}
    changes: dict[Staff, list[Glyph]] = {}
    for staff in own:
        found = clefs_on(frame.page.sheet, staff, frame.nearest)
        if not found or found[0].x > frame.system.bars[0] + 20:
            msg = f"{where}: {name} opens its system without a clef"
            raise Unreadable(msg)
        opening[staff], changes[staff] = found[0], found[1:]
    return opening, changes


def _part_key(
    reading: Reading,
    name: str,
    own: list[Staff],
    frame: _SystemFrame,
    opening: dict[Staff, Glyph],
) -> tuple[int, list[list[Glyph]]]:
    where = f"page {frame.system.page + 1}"
    signatures = [key_of(frame.page.sheet.music, staff, opening[staff]) for staff in own]
    keys = {sum(KEY_ACCIDENTALS[g.code] for g in signature) for signature in signatures}
    if len(keys) != 1 or (name in reading.keys and reading.keys[name] not in keys):
        msg = f"{where}: {name} prints keys {sorted(keys)} after {reading.keys.get(name)}, a key change no rule reads"
        raise Unreadable(
            msg,
        )
    return keys.pop(), signatures


def _bar_clefs(reading: Reading, part: _Part, bar: Bar, at: int, edge: tuple[float, float]) -> None:
    lo, hi = edge
    extra = part.frame.page.sheet.extra
    for number, staff in enumerate(part.own, 1):
        clef = clef_of(part.clef_at(staff, lo + 0.5) if at else part.opening[staff], staff, extra)[0]
        if reading.clefs.get((part.name, number)) != clef or part.frame.first + at == 1:
            bar.clefs.append((number, F(0), clef))
        for change in part.changes[staff]:
            if lo <= change.x < hi - 0.3:
                bar.clefs.append((number, F(-1), clef_of(change, staff, extra)[0]))
        reading.clefs[part.name, number] = clef_of(part.clef_at(staff, hi), staff, extra)[0]


def _staff_events(
    part: _Part,
    state: dict[tuple[int, str, int], int],
    number: int,
    found: list[list[Sound | Rest]],
) -> list[list[Event]]:
    pitching = _Pitching(
        part.own,
        part.frame.nearest,
        part.clef_at,
        part.frame.page.sheet.extra,
        part.accidentals,
        part.key,
        state,
        number if len(part.own) > 1 else None,
        part.frame.length,
    )
    return [[event_of(item, pitching) for item in voice] for voice in found]


def _clef_onsets(part: _Part, bar: Bar, edge: tuple[float, float]) -> None:
    lo, hi = edge
    extra = part.frame.page.sheet.extra
    for at_clef, (number, onset, clef) in enumerate(bar.clefs):
        if onset < 0:
            mine = list((bar.voices if number == 1 else bar.voices2)[0])
            change_x = next(
                g.x
                for g in part.changes[part.own[number - 1]]
                if lo <= g.x < hi - 0.3 and clef_of(g, part.own[number - 1], extra)[0] == clef
            )
            bar.clefs[at_clef] = (number, next((e.onset for e in mine if e.x > change_x), part.frame.length), clef)


def _read_bar(reading: Reading, part: _Part, at: int, edge: tuple[float, float]) -> Bar:
    lo, hi = edge
    frame = part.frame
    own = part.own
    bar = empty_bar(frame.first + at, frame.length)
    bar.left, bar.right = lo, hi
    state: dict[tuple[int, str, int], int] = {}
    _bar_clefs(reading, part, bar, at, edge)
    where = f"page {frame.system.page + 1}"
    window = _Window(lo, hi, frame.length, f"{where} bar {frame.first + at} {part.name}")
    voices = [
        _staff_events(part, state, number, found)
        for number, found in enumerate(staff_voices(own, part.sounds, part.rests, window), 1)
    ]
    for voice in [v for staff_voices in voices for v in staff_voices]:
        onset = F(0)
        for event in voice:
            event.onset = onset
            onset += event.length
    bar.voices = voices[0]
    if len(own) > 1:
        bar.voices2 = voices[1]
    _clef_onsets(part, bar, edge)
    curves_into(
        bar,
        [
            c
            for c in frame.page.sheet.curves
            if lo <= c[0] < hi and any(s.top - 8 * s.step <= c[1] <= s.bottom + 8 * s.step for s in own)
        ],
        own[0].step,
    )
    return bar


def read_part(reading: Reading, name: str, own: list[Staff], frame: _SystemFrame) -> None:
    system, sheet = frame.system, frame.page.sheet
    opening, changes = _opening_clefs(name, own, frame)
    staff_sounds = [s for s in frame.page.sounds if s.staff in own and system.bars[0] <= s.x < system.bars[-1]]
    staff_rests = [r for r in frame.page.rests if r.staff in own and system.bars[0] <= r.x < system.bars[-1]]
    key, signatures = _part_key(reading, name, own, frame, opening)
    reading.keys[name] = key
    accidentals = [g for g in sheet.music if g.code in ACCIDENTALS and not any(g in signature for signature in signatures)]
    part = _Part(name, own, frame, opening, changes, staff_sounds, staff_rests, key, accidentals)
    bars = reading.parts[name]
    for at, edge in enumerate(frame.edges):
        bars.append(_read_bar(reading, part, at, edge))


def staff_voices(
    own: list[Staff],
    staff_sounds: list[Sound],
    staff_rests: list[Rest],
    window: _Window,
) -> list[list[list[Sound | Rest]]]:
    """Read each staff's voices in 1 bar.

    Where a braced pair's staves each fall short and together read as 1 line, that line goes on the 1st staff and
    nothing on the 2nd.

    Returns:
        Each staff's voices.

    Raises:
        Unreadable: a staff's voices fill no bar, and the pair read together does not make 1 line.
    """
    lo, hi, length, where = window.lo, window.hi, window.length, window.where
    found: list[list[list[Sound | Rest]]] = []
    try:
        for number, staff in enumerate(own, 1):
            here = [s for s in staff_sounds if s.staff == staff and lo <= s.x < hi - 0.3]
            quiet = [r for r in staff_rests if r.staff == staff and lo <= r.x < hi - 0.3]
            voices = voices_of(here, quiet, length, f"{where}{' staff 2' if number == SECOND_STAFF else ''}")
            found.append(voices if voices != [[]] else [[Rest(staff, lo, staff.middle, None, None)]])
    except Unreadable:
        if len(own) < BRACED_PAIR:
            raise
        here = [s for s in staff_sounds if lo <= s.x < hi - 0.3]
        quiet = [r for r in staff_rests if lo <= r.x < hi - 0.3]
        crossing = voices_of(here, quiet, length, f"{where} across both staves")
        if len(crossing) != 1:
            raise
        return [crossing, []]
    return found


def event_of(item: Sound | Rest, pitching: _Pitching) -> Event:
    """1 sound or rest as the model's event, each head pitched from the staff it sits on and the clef there.

    Returns:
        The model's event.
    """
    own, nearest, number = pitching.own, pitching.nearest, pitching.number
    state = pitching.state
    if isinstance(item, Rest):
        event = (
            Event(item.x, [], pitching.length, bar_rest=True, hidden=item.hidden)
            if item.nominal is None
            else Event(item.x, [], item.nominal, 0, hidden=item.hidden)
        )
    else:
        heads = []
        for g in item.heads:
            staff = nearest(g.y)
            if staff not in own:
                staff = min(own, key=lambda s: abs(s.middle - g.y))
            step, octave = pitch_of(g.y, staff, clef_of(pitching.clef_at(staff, g.x), staff, pitching.extra)[1])
            mark = [
                a for a in pitching.accidentals if 0 < g.x - a.x < ACCIDENTAL_REACH and abs(a.y - g.y) < staff.step * 0.6
            ]
            accidental = ACCIDENTALS[max(mark, key=lambda a: a.x).code] if mark else None
            where = (own.index(staff), step, octave)
            if accidental is not None:
                state[where] = {"#": 1, "b": -1, "n": 0}[accidental]
            heads.append(
                Head(
                    g.x,
                    g.y,
                    step,
                    octave,
                    accidental,
                    "diamond" if g.code == DIAMOND_WHOLE else "normal",
                    alter=state.get(where, key_alter(step, pitching.key)),
                ),
            )
        event = Event(
            item.x,
            sorted(heads, key=lambda h: -h.y),
            item.nominal,
            item.dots,
            {"u": "up", "d": "down"}.get(item.direction),
            beam=item.beam,
        )
        if number is not None:
            event.staff = collections.Counter(
                own.index(nearest(g.y)) + 1 if nearest(g.y) in own else number for g in item.heads
            ).most_common(1)[0][0]
    if item.ratio is not None:
        event.tuplet, event.ratio = True, item.ratio
    if number is not None and event.staff is None:
        event.staff = number
    return event


def key_of(music: list[Glyph], staff: Staff, clef: Glyph) -> list[Glyph]:
    """Find a staff's key signature: the run of sharps or flats just after its opening clef, each close beside the last.

    Returns:
        The key signature's accidentals, left to right.
    """
    run: list[Glyph] = []
    for g in sorted(
        (
            g
            for g in music
            if g.code in KEY_ACCIDENTALS
            and staff.top - staff.step * 3 <= g.y <= staff.bottom + staff.step * 3
            and g.x > clef.x
        ),
        key=lambda g: g.x,
    ):
        if (not run and g.x - clef.x < KEY_START) or (run and g.code == run[0].code and g.x - run[-1].x < KEY_SPACING):
            run.append(g)
        else:
            break
    return run


def empty_bar(number: int, length: F) -> Bar:
    bar = Bar(number, 0, 0.0, 0.0, length)
    bar.voices = [[Event(0.0, [], length, bar_rest=True)]]
    return bar


def curves_into(bar: Bar, curves: list[tuple[float, float, float, float]], step: float) -> None:
    """Open a tie on a curve from a head to a head of the same pitch, or to the end of its line.

    A curve between 2 pitches is a slur.
    """
    heads = [h for voices in (bar.voices, bar.voices2) for voice in voices for event in voice for h in event.heads]
    for x0, y0, x1, y1 in curves:
        left = [h for h in heads if TIE_LEFT_BEFORE <= h.x - x0 <= TIE_LEFT_AFTER and abs(h.y - y0) < 3 * step]
        right = [
            h
            for h in heads
            if TIE_RIGHT_BEFORE <= h.x - x1 <= TIE_RIGHT_AFTER and h.x > x0 + 2 and abs(h.y - y1) < 3 * step
        ]
        if not left:
            continue
        start = min(left, key=lambda h: abs(h.x - x0) + abs(h.y - y0))
        end = min(right, key=lambda h: abs(h.x - x1) + abs(h.y - y1)) if right else None
        if end is None or (end.step, end.octave) == (start.step, start.octave):
            start.tie_start = True
        else:
            start.slur_start, end.slur_stop = True, True


def _close_stream(stream: list[Event]) -> None:
    for event, following in itertools.pairwise(stream):
        for head in event.heads:
            if not head.tie_start:
                continue
            match = [h for h in following.heads if (h.step, h.octave) == (head.step, head.octave)]
            if match:
                match[0].tie_stop = True
                if match[0].accidental is None:
                    match[0].alter = head.alter
            else:
                head.tie_start = False


def close_ties(bars: list[Bar]) -> None:
    """End each opened tie on the same pitch in the voice's next event, which keeps the tied note's alteration.

    With no such pitch the curve was a slur.
    """
    for staff in (0, 1):
        for voice_at in range(max(len(bar.voices if staff == 0 else bar.voices2) for bar in bars)):
            stream = [
                event
                for bar in bars
                for event in ((bar.voices if staff == 0 else bar.voices2)[voice_at : voice_at + 1] or [[]])[0]
            ]
            _close_stream(stream)


def finish(reading: Reading) -> Orchestra:
    if reading.time is None:
        msg = "no system read"
        raise Unreadable(msg)
    parts = []
    for name in reading.order:
        bars = reading.parts[name]
        if len(bars) != len(reading.frames):
            msg = f"{name} holds {len(bars)} bars of {len(reading.frames)}"
            raise Unreadable(msg)
        for bar, (page, opens_system, opens_page) in zip(bars, reading.frames, strict=True):
            bar.page, bar.new_system, bar.new_page = page, opens_system, opens_page
            if (
                reading.staves[name] == BRACED_PAIR
                and not bar.voices2
                and not any(event.staff == SECOND_STAFF for voice in bar.voices for event in voice)
            ):
                bar.voices2 = [[Event(0.0, [], bar.length, bar_rest=True, staff=2)]]
        bars[0].key, bars[0].time = reading.keys[name], reading.time
        close_ties(bars)
        parts.append(
            Part(
                name,
                reading.abbreviations.get(name, name),
                reading.staves[name],
                transposition_of(name),
                reading.keys[name],
                bars,
            ),
        )
    return Orchestra(parts)
