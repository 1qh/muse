"""Everything about a song that is derived rather than declared: decoder, key, target key, title."""

from __future__ import annotations

import dataclasses
import json
import operator
import os
import pathlib
import re

import pymupdf

from muse import bravura, leland, orchestral, symbolfont
from muse.model import Orchestra, Score, UnknownEncoding
from muse.smufl import PRIVATE_USE

MAJOR = {
    "Cb": -7,
    "Gb": -6,
    "Db": -5,
    "Ab": -4,
    "Eb": -3,
    "Bb": -2,
    "F": -1,
    "C": 0,
    "G": 1,
    "D": 2,
    "A": 3,
    "E": 4,
    "B": 5,
    "F#": 6,
    "C#": 7,
}
MINOR = {
    "Ab": -7,
    "Eb": -6,
    "Bb": -5,
    "F": -4,
    "C": -3,
    "G": -2,
    "D": -1,
    "A": 0,
    "E": 1,
    "B": 2,
    "F#": 3,
    "C#": 4,
    "G#": 5,
    "D#": 6,
    "A#": 7,
}
PITCH = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
TARGETS = {True: (("Em", "E", 1), ("Am", "A", 0)), False: (("G", "G", 1), ("C", "C", 0))}
KEY = re.compile(r"([A-G][b#]?)(m?)")
OVERRIDES = "titles.json"
MUSIC_FONTS = ("Anastasia", "Bravura")
NAME_AND_KEY = 2


class SongRefused(ValueError):
    """A source whose name and contents disagree, or that names no usable key or title."""


@dataclasses.dataclass(frozen=True)
class Song:
    name: str
    source_key: str
    source_fifths: int
    tone: str
    fifths: int
    direction: str
    semis: int
    title: str
    orchestral: bool = False

    @property
    def score_file(self) -> str:
        return f"{self.name}/{self.name}-{self.source_key}.mscz"

    @property
    def original_pdf(self) -> str:
        return f"{self.name}/{self.name}-{self.source_key}.pdf"

    @property
    def tone_pdf(self) -> str:
        """The PDF in the favourite tone; the original itself when it already is that tone."""
        return f"{self.name}/{self.name}-{self.tone}.pdf"

    @property
    def piano_pdf(self) -> str:
        return f"{self.name}/{self.name}-{self.tone}-piano.pdf"

    def files(self) -> set[str]:
        """List exactly what the song's folder holds.

        Returns:
            The folder's file paths.
        """
        if self.orchestral:
            return {self.score_file, self.original_pdf}
        return {self.score_file, self.original_pdf, self.tone_pdf, self.piano_pdf}


PAST_EVENT = 8.0
CHORD_LETTER = 5.5
CROSSING = 2.0


def sources(folders: pathlib.Path) -> list[pathlib.Path]:
    """Find every source PDF of 1 folder, or of several joined by the path separator; a song name appears once.

    Returns:
        The source PDFs, sorted by name.

    Raises:
        SongRefused: a song name repeats across the folders.
    """
    found = sorted(
        (pdf for folder in str(folders).split(os.pathsep) for pdf in pathlib.Path(folder).glob("*.pdf")),
        key=lambda p: p.name,
    )
    names = [pdf.stem.rsplit("-", 1)[0] for pdf in found]
    repeated = {name for name in names if names.count(name) > 1}
    if repeated:
        msg = f"song names repeat across folders: {sorted(repeated)}"
        raise SongRefused(msg)
    return found


def encoding(path: pathlib.Path) -> str:
    """Pick which decoder reads this PDF, judged by its fonts and music glyphs; an unknown one is refused by name.

    Returns:
        The decoder's name.

    Raises:
        UnknownEncoding: no decoder knows the PDF's fonts or glyphs.
    """
    doc = pymupdf.open(path)
    fonts = sorted({font[3] for page in doc for font in page.get_fonts()})
    if "Leland" in {name.split("+")[-1] for name in fonts}:
        return "leland"
    if any(name.split("+")[-1].startswith("Bravura") for name in fonts):
        return "bravura"
    if any(name.split("+")[-1].startswith("Anastasia") for name in fonts):
        return "symbolfont"
    codes = {ord(c) for page in doc for c in page.get_text()}
    if {symbolfont.STAFF, 0xF026} <= codes:
        return "symbolfont"
    msg = f"{path.name}: unknown music encoding, fonts {fonts}"
    raise UnknownEncoding(msg)


def settle_chord_bars(score: Score) -> Score:
    """Move each chord past every note and rest of its bar, whose name crosses the barline, to the next bar's first note.

    Returns:
        The same score, its chords settled.
    """
    for bar, following in zip(score.bars, score.bars[1:], strict=False):
        if following.new_system or following.new_page:
            continue
        events = [event.x for voice in bar.voices for event in voice if not event.bar_rest]
        for chord in list(bar.chords):
            name = (
                len(chord.root)
                + (1 if chord.root_alter else 0)
                + len(chord.kind)
                + (1 + len(chord.bass) if chord.bass is not None and chord.bass else 0)
            )
            taken = bool(following.chords) and min(c.x for c in following.chords) - following.left < PAST_EVENT
            if (
                events
                and max(events) < chord.x - PAST_EVENT
                and chord.x + name * CHORD_LETTER > bar.right + CROSSING
                and not taken
            ):
                bar.chords.remove(chord)
                following.chords.insert(0, chord)
    return score


def is_orchestral(path: pathlib.Path) -> bool:
    """Tell whether a source is a full score: the folder it sits in says so.

    Returns:
        Whether the source sits in the full-score folder.
    """
    return path.parent.name == orchestral.FOLDER


def orchestra_of(path: pathlib.Path) -> Orchestra:
    """Decode a full score's every part, its mode from the source's file name.

    Returns:
        The full score.

    Raises:
        SongRefused: the source is not in the full-score folder.
    """
    if not is_orchestral(path):
        msg = f"{path.name}: not in the {orchestral.FOLDER} folder, so not a full score"
        raise SongRefused(msg)
    found = orchestral.decode(path)
    key = KEY.fullmatch(path.stem.rsplit("-", 1)[-1])
    found.minor = None if key is None else key[2] == "m"
    return found


def decode(path: pathlib.Path) -> Score:
    if is_orchestral(path):
        msg = f"{path.name}: a full score is read by orchestra_of, never as a lead sheet"
        raise SongRefused(msg)
    decoders = {"bravura": bravura.decode, "leland": leland.decode, "symbolfont": symbolfont.decode}
    score = settle_chord_bars(decoders[encoding(path)](str(path)))
    found = KEY.fullmatch(path.stem.rsplit("-", 1)[-1])
    score.minor = None if found is None else found[2] == "m"
    for bar in score.bars:
        bar.source_line = bar.new_system
    return score


def target_of(root: str, minor: bool) -> tuple[str, int, str, int]:
    """Find the nearest target key by semitones.

    Returns:
        The target's name, fifths, direction and signed semitones.
    """
    pitch = PITCH[root[0]] + {"": 0, "b": -1, "#": 1}[root[1:]]
    options = []
    for name, target_root, fifths in TARGETS[minor]:
        semis = (PITCH[target_root] - pitch + 6) % 12 - 6
        options.append((abs(semis), name, fifths, semis))
    _, name, fifths, semis = min(options)
    return name, fifths, "up" if semis > 0 else "down" if semis < 0 else "", semis


def title_of(path: pathlib.Path) -> str:
    """Read the largest text on page 1, title-cased when the sheet prints it in capitals.

    Returns:
        The title.

    Raises:
        SongRefused: page 1 carries no title text.
    """
    spans = []
    for block in pymupdf.open(path)[0].get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                name = span["font"].split("+")[-1]
                has_letter = any(c.isalpha() and ord(c) < PRIVATE_USE.start for c in span["text"])
                if has_letter and not name.startswith(MUSIC_FONTS):
                    spans.append((span["size"], span["text"].strip()))
    if not spans:
        msg = f"{path.name}: no title text; add it to {OVERRIDES}"
        raise SongRefused(msg)
    text = max(spans, key=operator.itemgetter(0))[1]
    return str(text.title() if text.isupper() else text)


def song_of(path: pathlib.Path, score: Score | None = None) -> Song:
    parts = path.stem.rsplit("-", 1)
    found = KEY.fullmatch(parts[-1]) if len(parts) == NAME_AND_KEY else None
    if not found:
        msg = f"{path.name}: name must end in -<key>, such as Name-Cm"
        raise SongRefused(msg)
    root, minor = found[1], found[2] == "m"
    table = MINOR if minor else MAJOR
    if root not in table:
        msg = f"{path.name}: {parts[-1]} is not a usable key"
        raise SongRefused(msg)
    full = is_orchestral(path)
    if full:
        concert_key_problem(path, orchestra_of(path), table[root], parts[-1])
    else:
        _printed_key_problem(path, score, table[root], parts[-1])
    tone, fifths, direction, semis = target_of(root, minor)
    overrides_path = path.parent / OVERRIDES
    overrides = json.loads(overrides_path.read_text(encoding="utf-8")) if overrides_path.exists() else {}
    title = str(overrides.get(parts[0]) or title_of(path))
    return Song(parts[0], parts[-1], table[root], tone, fifths, direction, semis, title, full)


def _printed_key_problem(path: pathlib.Path, score: Score | None, fifths: int, named: str) -> None:
    decoded = score or decode(path)
    printed = decoded.bars[0].key if decoded.bars else None
    if printed != fifths:
        msg = f"{path.name}: name says {named} ({fifths} fifths), the sheet prints {printed} fifths"
        raise SongRefused(
            msg,
        )


def concert_key_problem(path: pathlib.Path, score: Orchestra, fifths: int, named: str) -> None:
    """Refuse a full score whose parts disagree with the concert key its name states.

    Each part sounding as written prints that key, or no key at all as timpani do,
    and each transposing part prints it moved by its transposition.

    Raises:
        SongRefused: a part prints another key, or no part prints the named one.
    """
    wrong = [
        f"{part.name} {part.key}"
        for part in score.parts
        if part.key != orchestral.written_key(fifths, part.transposition)
        and not (part.transposition is None and part.key == 0)
    ]
    if wrong or not any(part.transposition is None and part.key == fifths for part in score.parts):
        msg = f"{path.name}: name says {named} ({fifths} fifths), parts print {wrong or 'no such key'}"
        raise SongRefused(msg)
