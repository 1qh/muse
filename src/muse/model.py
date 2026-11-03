"""Score model shared by every source decoder and the MusicXML writer."""
from __future__ import annotations

import dataclasses
from fractions import Fraction as F

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
    heads: list[Head]  # empty means rest
    nominal: F  # written length in quarters, dots included
    dots: int = 0
    stem: str | None = None
    tuplet: bool = False
    beam: int | None = None
    hidden: bool = False
    bar_rest: bool = False
    trill: bool = False
    accent: bool = False
    fermata: bool = False
    grace: bool = False
    beam_state: str | None = None
    tuplet_state: str | None = None
    onset: F = F(0)

    @property
    def length(self) -> F:
        if self.grace:
            return F(0)
        return self.nominal * F(2, 3) if self.tuplet else self.nominal


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
    runs: list[tuple[str, bool, bool]]  # text, bold, italic
    above: bool
    kind: str = "cue"
    size: float = 10.0


@dataclasses.dataclass
class Bar:
    number: int
    page: int
    left: float
    right: float
    length: F
    time: tuple[int, int] | None = None
    time_symbol: str | None = None
    key: int | None = None
    new_system: bool = False
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
    tempo: tuple[str, int] | None = None
    wedges: list[tuple[float, float, str]] = dataclasses.field(default_factory=list)
    simile: bool = False
    clef_octave: int = 0
    base: float = 0.0


@dataclasses.dataclass
class Issue:
    bar: int
    message: str


@dataclasses.dataclass
class Score:
    bars: list[Bar]
    corrections: list[Issue]
    flags: list[Issue]
    has_second_staff: bool = False
    clef_octave: int = 0
