"""Score model shared by every source decoder and the MusicXML writer."""

from __future__ import annotations

import dataclasses
from fractions import Fraction

LETTERS = "CDEFGAB"


class UnknownEncoding(ValueError):
    """A PDF no decoder can read."""


class UnknownGlyph(ValueError):
    """A music symbol nobody has decoded; reading past it would silently lose music."""


@dataclasses.dataclass
class Head:
    x: float
    y: float
    step: str
    octave: int
    accidental: str | None = None
    shape: str = "normal"
    tie_start: bool = False
    tie_stop: bool = False
    slur_start: bool = False
    slur_stop: bool = False
    alter: int = 0


@dataclasses.dataclass
class Event:
    x: float
    heads: list[Head]
    nominal: Fraction
    dots: int = 0
    stem: str | None = None
    tuplet: bool = False
    beam: int | None = None
    hidden: bool = False
    bar_rest: bool = False
    trill: bool = False
    accent: bool = False
    fermata: bool = False
    marks: list[str] = dataclasses.field(default_factory=list)
    grace: bool = False
    beam_state: str | None = None
    tuplet_state: str | None = None
    onset: Fraction = Fraction(0)
    ratio: Fraction = Fraction(2, 3)
    staff: int | None = None

    @property
    def length(self) -> Fraction:
        if self.grace:
            return Fraction(0)
        return self.nominal * self.ratio if self.tuplet else self.nominal


@dataclasses.dataclass
class Chord:
    x: float
    root: str
    root_alter: int
    kind: str
    bass: str | None = None
    bass_alter: int = 0


@dataclasses.dataclass
class Text:
    x: float
    y: float
    runs: list[tuple[str, bool, bool]]
    above: bool
    kind: str = "cue"
    size: float = 10.0


Clef = tuple[str, int, int]


@dataclasses.dataclass
class Bar:
    number: int
    page: int
    left: float
    right: float
    length: Fraction
    time: tuple[int, int] | None = None
    time_symbol: str | None = None
    key: int | None = None
    new_system: bool = False
    source_line: bool = False
    new_page: bool = False
    voices: list[list[Event]] = dataclasses.field(default_factory=list)
    voices2: list[list[Event]] = dataclasses.field(default_factory=list)
    chords: list[Chord] = dataclasses.field(default_factory=list)
    texts: list[Text] = dataclasses.field(default_factory=list)
    texts2: list[Text] = dataclasses.field(default_factory=list)
    repeat_start: bool = False
    repeat_end: bool = False
    final: bool = False
    double: bool = False
    volta: str | None = None
    volta_stop: str | bool = False
    segno: bool = False
    coda: bool = False
    to_coda: bool = False
    dal_segno: bool = False
    fine: bool = False
    rehearsal: list[str] = dataclasses.field(default_factory=list)
    tempo: tuple[str, float] | None = None
    wedges: list[tuple[float, float, str]] = dataclasses.field(default_factory=list)
    simile: bool = False
    clef_octave: int = 0
    base: float = 0.0
    clefs: list[tuple[int, Fraction, Clef]] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class Issue:
    bar: int
    message: str


def bar_at(bars: list[Bar], x: float, lead: float) -> Bar:
    """Find the bar a mark printed at x belongs to: the last bar whose lead-in has begun.

    A mark starting just before a barline goes to the bar it runs into; the first bar for a mark left of every lead-in.

    Returns:
        The bar the mark belongs to.
    """
    begun = [bar for bar in bars if bar.left - lead <= x]
    return begun[-1] if begun else bars[0]


MARK_REACH = 6.0


def mark_bar(bars: list[Bar], left: float, right: float) -> Bar:
    """Find the bar a section label marks.

    Of the barlines under the label or within a few points of it, the one nearest its middle,
    since an engraver sets a section box on, just before or just after the barline it opens; else the bar it sits in.

    Returns:
        The bar the label marks.
    """
    middle = (left + right) / 2
    near = [bar for bar in bars[1:] if left - MARK_REACH <= bar.left <= right + MARK_REACH]
    if near:
        return min(near, key=lambda bar: abs(bar.left - middle))
    return bar_at(bars, middle, 0.0)


@dataclasses.dataclass
class Score:
    bars: list[Bar]
    corrections: list[Issue]
    flags: list[Issue]
    has_second_staff: bool = False
    clef_octave: int = 0
    minor: bool | None = None


@dataclasses.dataclass(frozen=True)
class Transposition:
    """How a part's written pitch moves to sound: MusicXML's diatonic steps, chromatic semitones and octaves."""

    diatonic: int
    chromatic: int
    octave: int = 0


@dataclasses.dataclass
class Part:
    """1 instrument of a full score across every bar, printed on 1 staff or 2."""

    name: str
    abbreviation: str
    staves: int
    transposition: Transposition | None
    key: int
    bars: list[Bar]


@dataclasses.dataclass
class Orchestra:
    """A full score: every part in the source's staff order, all holding every bar."""

    parts: list[Part]
    minor: bool | None = None
