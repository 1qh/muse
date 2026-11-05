"""Short chord names: each stored as a spelling the score programs parse, so it prints short and plays like the long form."""
from __future__ import annotations

import json
import pathlib
import struct
import subprocess
from collections.abc import Callable

SHORT = {
    "maj7": "^7",
    "maj9": "^9",
    "add2": "2",
    "add9": "2",
    "(add2)": "2",
    "(add9)": "2",
    "madd2": "m2",
    "madd9": "m2",
    "m(add2)": "m2",
    "m(add9)": "m2",
    "sus4": "sus",
    "m7(b5)": "0",
    "dim": "o",
    "dim7": "o7",
    "7(b9)": "7b9",
    "m7(add4)": "m7add4",
}
LEGACY_SPELLING = {"0": "07"}  # a bare half-diminished does not parse in MuseScore 3
# pitch classes on a root of A, each proved against the long form in both programs
PLAYS = {
    "^7": [1, 4, 8, 9],
    "^9": [1, 4, 8, 9, 11],
    "2": [1, 4, 9, 11],
    "m2": [0, 4, 9, 11],
    "sus": [2, 4, 9],
    "0": [0, 3, 7, 9],
    "07": [0, 3, 7, 9],
    "o": [0, 3, 9],
    "o7": [0, 3, 6, 9],
    "7b9": [1, 4, 7, 9, 10],
    "m7add4": [0, 2, 4, 7, 9],
}
CHORD_FILE_RENDERS = (("<render>circle</render>", "<render>\u00b0</render>"), ("<render>triangle</render>", "<render>\u0394</render>"))
SHIPPED_CHORD_FILE = pathlib.Path.home() / "Applications" / "MuseScore 3.app" / "Contents" / "Resources" / "styles" / "chords_std.xml"
Runner = Callable[..., None]


def stored(kind: str) -> str:
    """The spelling a chord kind is written with."""
    return SHORT.get(kind, kind)


def same_chord(left: str, right: str) -> bool:
    """Whether 2 stored names mean 1 chord, whichever program wrote them."""
    return LEGACY_SPELLING.get(left, left) == LEGACY_SPELLING.get(right, right)


def chord_file(work: pathlib.Path) -> pathlib.Path:
    """The program's own chord description, printing the circle and triangle as SF Pro letters."""
    text = SHIPPED_CHORD_FILE.read_text(encoding="utf-8")
    for old, new in CHORD_FILE_RENDERS:
        if old not in text:
            raise RuntimeError(f"{SHIPPED_CHORD_FILE} has no {old}")
        text = text.replace(old, new)
    path = work / "chords_short.xml"
    work.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.read_text(encoding="utf-8") != text:
        path.write_text(text, encoding="utf-8")
    return path


def bars_xml(kinds: list[str]) -> str:
    """1 bar of A per chord kind."""
    bars = []
    for number, kind in enumerate(kinds, 1):
        head = '<attributes><divisions>1</divisions><key><fifths>0</fifths></key><time><beats>4</beats><beat-type>4</beat-type></time><clef><sign>G</sign><line>2</line></clef></attributes>' if number == 1 else ""
        bars.append(f'<measure number="{number}">{head}<harmony><root><root-step>A</root-step></root><kind text="{kind}">other</kind></harmony><note><rest measure="yes"/><duration>4</duration></note></measure>')
    return '<?xml version="1.0" encoding="UTF-8"?><score-partwise version="3.1"><part-list><score-part id="P1"><part-name>x</part-name></score-part></part-list><part id="P1">' + "".join(bars) + "</part></score-partwise>"


def variable_length(track: bytes, at: int) -> tuple[int, int]:
    """A MIDI variable-length number and the position after it."""
    value = 0
    while True:
        byte = track[at]
        at += 1
        value = value << 7 | byte & 127
        if byte < 128:
            return value, at


def sounding(mid: pathlib.Path) -> dict[int, list[int]]:
    """Pitch classes sounding per 4-quarter bar, from a standard MIDI file."""
    data = mid.read_bytes()
    division = struct.unpack(">H", data[12:14])[0]
    at = 14
    found: dict[int, set[int]] = {}
    while at < len(data):
        size = struct.unpack(">I", data[at + 4:at + 8])[0]
        track = data[at + 8:at + 8 + size]
        at += 8 + size
        i = now = status = 0
        while i < len(track):
            delta, i = variable_length(track, i)
            now += delta
            if track[i] >= 128:
                status = track[i]
                i += 1
            if status == 255:
                length, i = variable_length(track, i + 1)
                i += length
            elif status in (240, 247):
                length, i = variable_length(track, i)
                i += length
            elif status >> 4 in (12, 13):
                i += 1
            else:
                pitch, velocity = track[i], track[i + 1]
                i += 2
                if status >> 4 == 9 and velocity:
                    found.setdefault(now // (division * 4), set()).add(pitch % 12)
    return {bar: sorted(pitches) for bar, pitches in found.items()}


def wrong_playback(kinds: list[str], heard: dict[int, list[int]]) -> list[str]:
    """Kinds whose bar does not sound as proved."""
    return [f"{kind}: plays {heard.get(bar)}, wanted {PLAYS[kind]}" for bar, kind in enumerate(kinds) if heard.get(bar) != PLAYS[kind]]


def playback_problems(command: str, style: pathlib.Path, work: pathlib.Path, legacy: bool, run: Runner) -> list[str]:
    """Build 1 bar per stored spelling in the given program with the sheets' own style, and compare its MIDI with the proved pitch classes."""
    kinds = sorted({LEGACY_SPELLING.get(value, value) if legacy else value for value in SHORT.values()})
    folder = work / ("playback3" if legacy else "playback4")
    folder.mkdir(parents=True, exist_ok=True)
    source = folder / "kinds.musicxml"
    source.write_text(bars_xml(kinds), encoding="utf-8")
    score = folder / "kinds.mscz"
    mid = folder / "kinds.mid"
    run([command, "-S", str(style), "-o", str(score), str(source)])
    run([command, "-o", str(mid), str(score)])
    return wrong_playback(kinds, sounding(mid))


def transposes(command: str, style: pathlib.Path, work: pathlib.Path, run: Runner) -> list[str]:
    """With the custom chord file, a moved score keeps every name, moves every root and still sounds as proved a tone up."""
    folder = work / "playback4"
    folder.mkdir(parents=True, exist_ok=True)
    kinds = sorted(set(SHORT.values()))
    source = folder / "moved.musicxml"
    source.write_text(bars_xml(kinds), encoding="utf-8")
    score = folder / "moved-start.mscz"
    moved = folder / "moved.mscz"
    mid = folder / "moved.mid"
    options = {"mode": "to_key", "direction": "up", "targetKey": 2, "transposeKeySignatures": True, "transposeChordNames": True, "useDoubleSharpsFlats": False, "transposeInterval": 0}
    run([command, "-S", str(style), "-o", str(score), str(source)])
    run([command, "-o", str(moved), "--transpose", json.dumps(options), str(score)])
    run([command, "-o", str(mid), str(moved)])
    heard = sounding(mid)
    return [f"{kind} a tone up: plays {heard.get(bar)}" for bar, kind in enumerate(kinds) if heard.get(bar) != sorted((pc + 2) % 12 for pc in PLAYS[kind])]


def checked_run(arguments: list[str]) -> None:
    subprocess.run(arguments, capture_output=True, check=True, timeout=240, stdin=subprocess.DEVNULL)
