"""Read what a saved MuseScore file actually holds, in either format, bar by bar."""
from __future__ import annotations

import dataclasses
import pathlib
import re
import xml.etree.ElementTree as ET
import zipfile
from fractions import Fraction as F

from muse import chordnames

DURATIONS = {"long": F(4), "breve": F(2), "whole": F(1), "half": F(1, 2), "quarter": F(1, 4), "eighth": F(1, 8), "16th": F(1, 16), "32nd": F(1, 32), "64th": F(1, 64)}
MARK_TAGS = ("Arpeggio", "Tremolo", "Fermata", "HairPin")  # marks counted by kind
GRACE = ("acciaccatura", "appoggiatura", "grace4", "grace16", "grace32", "grace8after", "grace16after", "grace32after")


@dataclasses.dataclass
class FileBar:
    pitches: list[int] = dataclasses.field(default_factory=list)
    chords: list[tuple[F, int, int, str]] = dataclasses.field(default_factory=list)  # quarters, root, bass, name
    signs: list[str] = dataclasses.field(default_factory=list)
    jumps: list[tuple[str, str, str]] = dataclasses.field(default_factory=list)
    marks: list[str] = dataclasses.field(default_factory=list)  # articulations, ornaments, dynamics and hairpins, by kind
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
    for tag in ("Articulation", "Ornament"):  # MuseScore 4 keeps a trill as an ornament, MuseScore 3 as an articulation: the subtype names it in both
        bar.marks += [re.sub(r"(Above|Below)$", "", element.findtext("subtype") or "") for element in measure.iter(tag)]  # where it prints is layout; what it is must match
    for dynamic in measure.iter("Dynamic"):
        bar.marks.append("dynamic " + (dynamic.findtext("subtype") or ""))
    for tag in MARK_TAGS:
        bar.marks += [tag] * sum(1 for _ in measure.iter(tag))
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


TRACKS = 4  # voices per staff, the unit a spanning arpeggio counts in


def read(path: pathlib.Path) -> list[list[FileBar]]:
    """1 list of bars per staff; an arpeggio spanning several staves counts on each staff it covers, as MuseScore 3 writes it."""
    found = staves(root_of(path))
    bars = [[read_bar(m) for m in staff.findall("Measure")] for staff in found]
    for number, staff in enumerate(found):
        for index, measure in enumerate(staff.findall("Measure")):
            for arpeggio in measure.iter("Arpeggio"):
                for below in range(1, (int(arpeggio.findtext("span") or 1) - 1) // TRACKS + 1):
                    if number + below < len(bars) and index < len(bars[number + below]):
                        bars[number + below][index].marks.append("Arpeggio")
    return bars


def same_spelling(chords: list[tuple[F, int, int, str]]) -> list[tuple[F, int, int, str]]:
    """Chords with the legacy file's parser spellings folded, since they name the same chord."""
    return sorted((at, root, bass, chordnames.LEGACY_SPELLING.get(name, name)) for at, root, bass, name in chords)


def difference(a: pathlib.Path, b: pathlib.Path) -> list[str]:
    """Where 2 files disagree about notes, chords, chord beats, signs, jumps or marks."""
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
            if same_spelling(x.chords) != same_spelling(y.chords):
                problems.append(f"bar {index}: chords differ {sorted(x.chords)} {sorted(y.chords)}")
            if sorted(x.signs) != sorted(y.signs):
                problems.append(f"bar {index}: signs differ {x.signs} {y.signs}")
            if sorted(x.jumps) != sorted(y.jumps):
                problems.append(f"bar {index}: jumps differ {x.jumps} {y.jumps}")
            if sorted(x.marks) != sorted(y.marks):
                problems.append(f"bar {index} staff {number}: marks differ {sorted(x.marks)} {sorted(y.marks)}")
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
    for element in [*root.iter("Marker"), *root.iter("Jump")]:
        found = element.find("text")
        words = "".join([found.text or "", *(child.tail or "" for child in found)]).strip() if found is not None else ""
        if words:
            problems.append(f"jump sign carries words: {words}")
        box = element.find("frameFgColor")
        kind = {s.text or "" for s in element.iter("sym")}
        if element.findtext("frameType") != "1" or box is None:
            problems.append(f"jump sign {sorted(kind)} has no box")
        elif (box.get("r") == "220") != ("segno" in kind) or (box.get("b") == "220") != ("coda" in kind):
            problems.append(f"jump sign {sorted(kind)} has the wrong box color")
    return problems


def articulate(path: pathlib.Path, marks: list[tuple[int, int, F, str]]) -> list[str]:
    """Write each (staff index, bar number, onset in quarters, articulation subtype) onto the chord there; the marks no chord stands at."""
    with zipfile.ZipFile(path) as z:
        members = {name: z.read(name) for name in z.namelist()}
    name = next(n for n in members if n.endswith(".mscx") and "/" not in n)
    root = ET.fromstring(members[name])
    lost = []
    found = staves(root)
    for staff, number, onset, subtype in marks:
        chord = chord_at(found[staff].findall("Measure")[number - 1], onset) if staff < len(found) and number <= len(found[staff].findall("Measure")) else None
        if chord is None:
            lost.append(f"staff {staff + 1} bar {number} beat {onset}: no chord for {subtype}")
            continue
        mark = ET.Element("Articulation")
        ET.SubElement(mark, "subtype").text = subtype
        first_note = next((at for at, child in enumerate(chord) if child.tag == "Note"), len(chord))
        chord.insert(first_note, mark)
    members[name] = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for member, data in members.items():
            z.writestr(member, data)
    return lost


def chord_at(measure: ET.Element, onset: F) -> ET.Element | None:
    """The chord a bar's voices start at onset quarters, read as `read_bar` reads them."""
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
            elif node.tag in ("Chord", "Rest"):
                if node.tag == "Chord" and at * 4 == onset:
                    return node
                at += length_of(node, scale)
    return None


TEXT_ELEMENTS = ("StaffText", "SystemText", "Tempo", "RehearsalMark", "Expression")


def empty_text_problems(path: pathlib.Path) -> list[str]:
    """Every text element with nothing to print: MuseScore still places it and reserves room for it."""
    problems = []
    for number, staff in enumerate(staves(root_of(path)), 1):
        for bar, measure in enumerate(staff.findall("Measure"), 1):
            for tag in TEXT_ELEMENTS:
                for element in measure.iter(tag):
                    shown = element.find("text")
                    if shown is None or ("".join(shown.itertext()).strip() or shown.find(".//sym") is not None):
                        continue
                    problems.append(f"staff {number} bar {bar}: an empty {tag}")
    return problems
