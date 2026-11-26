"""Short chord names: each stored as a spelling the score programs parse.

Each prints short and plays like the long form.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import struct
import tempfile
import zipfile
from collections.abc import Callable

from muse import launch

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
CHECK_SECONDS = 240
PLAYS = {
    "^7": [1, 4, 8, 9],
    "^9": [1, 4, 8, 9, 11],
    "2": [1, 4, 9, 11],
    "m2": [0, 4, 9, 11],
    "sus": [2, 4, 9],
    "0": [0, 3, 7, 9],
    "o": [0, 3, 9],
    "o7": [0, 3, 6, 9],
    "7b9": [1, 4, 7, 9, 10],
    "m7add4": [0, 2, 4, 7, 9],
}
CHORD_FILE = pathlib.Path(__file__).with_name("chords.xml")
Runner = Callable[..., None]
MIDI_STATUS_BIT = 128
MIDI_META_EVENT = 255
MIDI_NOTE_ON = 9


def stored(kind: str) -> str:
    """Spell a chord kind the way it is written.

    Returns:
        The stored spelling of the chord kind.

    """
    return SHORT.get(kind, kind)


CHORD_PATH = re.compile(rb"<chordDescriptionFile>[^<]*</chordDescriptionFile>")
STORED_NAME = b"<chordDescriptionFile>chordlist.xml</chordDescriptionFile>"


def drop_chord_path(mscz: pathlib.Path) -> None:
    """Name the embedded chord list in a score's style in place of the build machine's path to it."""
    with zipfile.ZipFile(mscz) as source:
        members = [(item, source.read(item)) for item in source.infolist()]
    with zipfile.ZipFile(mscz, "w", zipfile.ZIP_DEFLATED) as target:
        for item, data in members:
            target.writestr(item, CHORD_PATH.sub(STORED_NAME, data) if item.filename.endswith((".mss", ".mscx")) else data)


def chord_path_problems(mscz: pathlib.Path) -> list[str]:
    """Any style in a score that names a chord file by anything but the embedded list.

    Returns:
        A problem line per chord file a style names other than the embedded list.

    """
    with zipfile.ZipFile(mscz) as source:
        found = [
            m
            for name in source.namelist()
            if name.endswith((".mss", ".mscx"))
            for m in CHORD_PATH.findall(source.read(name))
        ]
    return [f"{mscz.name}: chord file {m.decode()}" for m in found if m != STORED_NAME and b"chords_std" not in m]


def chord_file(work: pathlib.Path) -> pathlib.Path:
    """Copy the pipeline's own chord description where a style names it.

    Returns:
        The path of the copied chord description.

    """
    path = work / "chords_short.xml"
    work.mkdir(parents=True, exist_ok=True)
    text = CHORD_FILE.read_text(encoding="utf-8")
    if not path.exists() or path.read_text(encoding="utf-8") != text:
        fd, staged = tempfile.mkstemp(prefix="chords_short-", suffix=".xml", dir=work)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        pathlib.Path(staged).replace(path)
    return path


def bars_xml(kinds: list[str]) -> str:
    """1 bar of A per chord kind.

    Returns:
        A MusicXML score holding 1 bar per chord kind.

    """
    bars = []
    for number, kind in enumerate(kinds, 1):
        head = (
            "<attributes><divisions>1</divisions><key><fifths>0</fifths></key>"
            "<time><beats>4</beats><beat-type>4</beat-type></time>"
            "<clef><sign>G</sign><line>2</line></clef></attributes>"
            if number == 1
            else ""
        )
        bars.append(
            f'<measure number="{number}">{head}<harmony><root><root-step>A</root-step></root>'
            f'<kind text="{kind}">other</kind></harmony>'
            '<note><rest measure="yes"/><duration>4</duration></note></measure>',
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?><score-partwise version="3.1">'
        '<part-list><score-part id="P1"><part-name>x</part-name></score-part></part-list><part id="P1">'
        + "".join(bars)
        + "</part></score-partwise>"
    )


def variable_length(track: bytes, at: int) -> tuple[int, int]:
    """Read a MIDI variable-length number and the position after it.

    Returns:
        The number and the position after it.

    """
    value = 0
    while True:
        byte = track[at]
        at += 1
        value = value << 7 | byte & 127
        if byte < MIDI_STATUS_BIT:
            return value, at


def sounding(mid: pathlib.Path) -> dict[int, list[int]]:
    """Pitch classes sounding per 4-quarter bar, from a standard MIDI file.

    Returns:
        The sorted pitch classes sounding in each bar.

    """
    data = mid.read_bytes()
    division = struct.unpack(">H", data[12:14])[0]
    at = 14
    found: dict[int, set[int]] = {}
    while at < len(data):
        size = struct.unpack(">I", data[at + 4 : at + 8])[0]
        track = data[at + 8 : at + 8 + size]
        at += 8 + size
        i = now = status = 0
        while i < len(track):
            delta, i = variable_length(track, i)
            now += delta
            if track[i] >= MIDI_STATUS_BIT:
                status = track[i]
                i += 1
            if status == MIDI_META_EVENT:
                length, i = variable_length(track, i + 1)
                i += length
            elif status in {240, 247}:
                length, i = variable_length(track, i)
                i += length
            elif status >> 4 in {12, 13}:
                i += 1
            else:
                pitch, velocity = track[i], track[i + 1]
                i += 2
                if status >> 4 == MIDI_NOTE_ON and velocity:
                    found.setdefault(now // (division * 4), set()).add(pitch % 12)
    return {bar: sorted(pitches) for bar, pitches in found.items()}


def wrong_playback(kinds: list[str], heard: dict[int, list[int]]) -> list[str]:
    """Kinds whose bar does not sound as proved.

    Returns:
        A problem line per kind whose bar sounds wrong.

    """
    return [
        f"{kind}: plays {heard.get(bar)}, wanted {PLAYS[kind]}"
        for bar, kind in enumerate(kinds)
        if heard.get(bar) != PLAYS[kind]
    ]


def playback_problems(command: str, style: pathlib.Path, work: pathlib.Path, run: Runner) -> list[str]:
    """Build 1 bar per stored spelling in the given program with the sheets' own style.

    Its MIDI is compared with the proved pitch classes.

    Returns:
        A problem line per kind whose bar sounds wrong.

    """
    kinds = sorted(set(SHORT.values()))
    work.mkdir(parents=True, exist_ok=True)
    folder = pathlib.Path(tempfile.mkdtemp(prefix="playback-", dir=work))
    source = folder / "kinds.musicxml"
    source.write_text(bars_xml(kinds), encoding="utf-8")
    score = folder / "kinds.mscz"
    mid = folder / "kinds.mid"
    try:
        run([command, "-S", str(style), "-o", str(score), str(source)])
        run([command, "-o", str(mid), str(score)])
        return wrong_playback(kinds, sounding(mid))
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def transposes(command: str, style: pathlib.Path, work: pathlib.Path, run: Runner) -> list[str]:
    """Check that with the custom chord file a moved score keeps every name and moves every root.

    It still sounds as proved a tone up.

    Returns:
        A problem line per kind that does not sound as proved a tone up.

    """
    work.mkdir(parents=True, exist_ok=True)
    folder = pathlib.Path(tempfile.mkdtemp(prefix="playback-moved-", dir=work))
    kinds = sorted(set(SHORT.values()))
    source = folder / "moved.musicxml"
    source.write_text(bars_xml(kinds), encoding="utf-8")
    score = folder / "moved-start.mscz"
    moved = folder / "moved.mscz"
    mid = folder / "moved.mid"
    options = {
        "mode": "to_key",
        "direction": "up",
        "targetKey": 2,
        "transposeKeySignatures": True,
        "transposeChordNames": True,
        "useDoubleSharpsFlats": False,
        "transposeInterval": 0,
    }
    try:
        run([command, "-S", str(style), "-o", str(score), str(source)])
        run([command, "-o", str(moved), "--transpose", json.dumps(options), str(score)])
        run([command, "-o", str(mid), str(moved)])
        heard = sounding(mid)
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    return [
        f"{kind} a tone up: plays {heard.get(bar)}"
        for bar, kind in enumerate(kinds)
        if heard.get(bar) != sorted((pc + 2) % 12 for pc in PLAYS[kind])
    ]


def checked_run(arguments: list[str]) -> None:
    """1 MuseScore run through the shared launch slots; a failed run raises."""
    launch.heavy(arguments, CHECK_SECONDS, check=True)
