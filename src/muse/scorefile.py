"""Read what a saved MuseScore file actually holds, in either format, bar by bar."""
from __future__ import annotations

import dataclasses
import pathlib
import xml.etree.ElementTree as ET
import zipfile
from fractions import Fraction as F

DURATIONS = {"long": F(4), "breve": F(2), "whole": F(1), "half": F(1, 2), "quarter": F(1, 4), "eighth": F(1, 8), "16th": F(1, 16), "32nd": F(1, 32), "64th": F(1, 64)}
GRACE = ("acciaccatura", "appoggiatura", "grace4", "grace16", "grace32", "grace8after", "grace16after", "grace32after")


@dataclasses.dataclass
class FileBar:
    pitches: list[int] = dataclasses.field(default_factory=list)
    chords: list[tuple[F, int, int, str]] = dataclasses.field(default_factory=list)  # quarters, root, bass, name
    signs: list[str] = dataclasses.field(default_factory=list)
    jumps: list[tuple[str, str, str]] = dataclasses.field(default_factory=list)
    length: F = F(0)


def root_of(path: pathlib.Path) -> ET.Element:
    with zipfile.ZipFile(path) as z:
        name = next(n for n in z.namelist() if n.endswith(".mscx") and "/" not in n)
        return ET.fromstring(z.read(name))


def staves(root: ET.Element) -> list[ET.Element]:
    score = root.find("Score")
    assert score is not None
    return [s for s in score.findall("Staff") if s.find("Measure") is not None]


def length_of(event: ET.Element, scale: F) -> F:
    if any(event.find(tag) is not None for tag in GRACE):
        return F(0)
    text = event.findtext("duration")
    if text and event.findtext("durationType") == "measure":
        return F(text)
    base = DURATIONS.get(event.findtext("durationType") or "", F(0))
    dots = int(event.findtext("dots") or 0)
    return base * (2 - F(1, 2 ** dots)) * scale


def read_bar(measure: ET.Element) -> FileBar:
    bar = FileBar()
    for marker in measure.iter("Marker"):
        bar.signs.append(marker.findtext("label") or "")
    for jump in measure.iter("Jump"):
        bar.jumps.append((jump.findtext("jumpTo") or "", jump.findtext("playUntil") or "", jump.findtext("continueAt") or ""))
    for voice in measure.findall("voice"):
        at = F(0)
        scale = F(1)
        for node in voice:
            if node.tag == "location":
                at += F(node.findtext("fractions") or "0")
            elif node.tag == "Tuplet":
                scale = F(int(node.findtext("normalNotes") or 1), int(node.findtext("actualNotes") or 1))
            elif node.tag == "endTuplet":
                scale = F(1)
            elif node.tag == "Harmony":
                info = node.find("harmonyInfo")
                holder = info if info is not None else node
                bar.chords.append((at * 4, int(holder.findtext("root") or -1), int(holder.findtext("base") or holder.findtext("bass") or -1), holder.findtext("name") or ""))
            elif node.tag in ("Chord", "Rest"):
                if node.tag == "Chord":
                    bar.pitches += [int(n.findtext("pitch") or 0) for n in node.findall("Note")]
                at += length_of(node, scale)
        bar.length = max(bar.length, at)
    return bar


def read(path: pathlib.Path) -> list[list[FileBar]]:
    """1 list of bars per staff."""
    return [[read_bar(m) for m in staff.findall("Measure")] for staff in staves(root_of(path))]


def difference(a: pathlib.Path, b: pathlib.Path) -> list[str]:
    """Where 2 files disagree about notes, chords, chord beats, signs or jumps."""
    one, two = read(a), read(b)
    problems = []
    if len(one) != len(two):
        return [f"{len(one)} staves against {len(two)}"]
    for number, (left, right) in enumerate(zip(one, two, strict=True), 1):
        if len(left) != len(right):
            problems.append(f"staff {number}: {len(left)} bars against {len(right)}")
            continue
        for index, (x, y) in enumerate(zip(left, right, strict=True), 1):
            if sorted(x.pitches) != sorted(y.pitches):
                problems.append(f"bar {index} staff {number}: notes differ")
            if sorted(x.chords) != sorted(y.chords):
                problems.append(f"bar {index}: chords differ {sorted(x.chords)} {sorted(y.chords)}")
            if sorted(x.signs) != sorted(y.signs):
                problems.append(f"bar {index}: signs differ {x.signs} {y.signs}")
            if sorted(x.jumps) != sorted(y.jumps):
                problems.append(f"bar {index}: jumps differ {x.jumps} {y.jumps}")
    return problems


def builtin_sign_problems(path: pathlib.Path) -> list[str]:
    """Segno and coda must be the application's own symbols, sized by its own size setting."""
    problems = []
    root = root_of(path)
    for marker in root.iter("Marker"):
        label = marker.findtext("label")
        if label not in ("segno", "codab"):
            continue
        text = marker.find("text")
        symbols = [s.text for s in text.iter("sym")] if text is not None else []
        wanted = "segno" if label == "segno" else "coda"
        if wanted not in symbols:
            problems.append(f"{label} is not the built-in sign")
        if root.get("version", "").startswith("4") and marker.findtext("markerType") != label:
            problems.append(f"{label} has no marker type, so it prints as an end-of-bar marker")
        if marker.find("family") is not None:
            problems.append(f"{label} forces a font")
    return problems
