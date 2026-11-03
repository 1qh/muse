"""Everything about a song that is derived rather than declared: decoder, key, target key, title."""
from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import re

import pymupdf

from muse import bravura, symbolfont
from muse.model import Score, UnknownEncoding

MAJOR = {"Cb": -7, "Gb": -6, "Db": -5, "Ab": -4, "Eb": -3, "Bb": -2, "F": -1, "C": 0, "G": 1, "D": 2, "A": 3, "E": 4, "B": 5, "F#": 6, "C#": 7}
MINOR = {"Ab": -7, "Eb": -6, "Bb": -5, "F": -4, "C": -3, "G": -2, "D": -1, "A": 0, "E": 1, "B": 2, "F#": 3, "C#": 4, "G#": 5, "D#": 6, "A#": 7}
PITCH = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
TARGETS = {True: (("Em", "E", 1), ("Am", "A", 0)), False: (("G", "G", 1), ("C", "C", 0))}
KEY = re.compile(r"([A-G][b#]?)(m?)")
OVERRIDES = "titles.json"
MUSIC_FONTS = ("Anastasia", "Bravura")


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


def sources(folders: pathlib.Path) -> list[pathlib.Path]:
    """Every source PDF of 1 folder, or of several joined by the path separator; a song name appears once."""
    found = sorted((pdf for folder in str(folders).split(os.pathsep) for pdf in pathlib.Path(folder).glob("*.pdf")), key=lambda p: p.name)
    names = [pdf.stem.rsplit("-", 1)[0] for pdf in found]
    repeated = {name for name in names if names.count(name) > 1}
    if repeated:
        raise SongRefused(f"song names repeat across folders: {sorted(repeated)}")
    return found


def encoding(path: pathlib.Path) -> str:
    """Which decoder reads this PDF, judged by its fonts and music glyphs; an unknown one is refused by name."""
    doc = pymupdf.open(path)
    fonts = sorted({font[3] for page in doc for font in page.get_fonts()})
    if any(name.split("+")[-1].startswith("Bravura") for name in fonts):
        return "bravura"
    if any(name.split("+")[-1].startswith("Anastasia") for name in fonts):
        return "symbolfont"
    codes = {ord(c) for page in doc for c in page.get_text()}
    if {symbolfont.STAFF, 0xF026} <= codes:
        return "symbolfont"
    raise UnknownEncoding(f"{path.name}: unknown music encoding, fonts {fonts}")


def decode(path: pathlib.Path) -> Score:
    return bravura.decode(str(path)) if encoding(path) == "bravura" else symbolfont.decode(str(path))


def target_of(root: str, minor: bool) -> tuple[str, int, str, int]:
    """Nearest target key by semitones: name, fifths, direction, signed semitones."""
    pitch = PITCH[root[0]] + {"": 0, "b": -1, "#": 1}[root[1:]]
    options = []
    for name, target_root, fifths in TARGETS[minor]:
        semis = (PITCH[target_root] - pitch + 6) % 12 - 6
        options.append((abs(semis), name, fifths, semis))
    _, name, fifths, semis = min(options)
    return name, fifths, "up" if semis > 0 else "down" if semis < 0 else "", semis


def title_of(path: pathlib.Path) -> str:
    """The largest text on page 1, title-cased when the sheet prints it in capitals."""
    spans = []
    for block in pymupdf.open(path)[0].get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                name = span["font"].split("+")[-1]
                has_letter = any(c.isalpha() and ord(c) < 0xE000 for c in span["text"])
                if has_letter and not name.startswith(MUSIC_FONTS):
                    spans.append((span["size"], span["text"].strip()))
    if not spans:
        raise SongRefused(f"{path.name}: no title text; add it to {OVERRIDES}")
    text = max(spans, key=lambda item: item[0])[1]
    return str(text.title() if text.isupper() else text)


def song_of(path: pathlib.Path, score: Score | None = None) -> Song:
    parts = path.stem.rsplit("-", 1)
    found = KEY.fullmatch(parts[-1]) if len(parts) == 2 else None
    if not found:
        raise SongRefused(f"{path.name}: name must end in -<key>, such as Name-Cm")
    root, minor = found[1], found[2] == "m"
    table = MINOR if minor else MAJOR
    if root not in table:
        raise SongRefused(f"{path.name}: {parts[-1]} is not a usable key")
    decoded = score or decode(path)
    printed = decoded.bars[0].key if decoded.bars else None
    if printed != table[root]:
        raise SongRefused(f"{path.name}: name says {parts[-1]} ({table[root]} fifths), the sheet prints {printed} fifths")
    tone, fifths, direction, semis = target_of(root, minor)
    overrides_path = path.parent / OVERRIDES
    overrides = json.loads(overrides_path.read_text(encoding="utf-8")) if overrides_path.exists() else {}
    title = str(overrides.get(parts[0]) or title_of(path))
    return Song(parts[0], parts[-1], table[root], tone, fifths, direction, semis, title)
