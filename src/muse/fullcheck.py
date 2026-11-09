"""The checks a full score takes: its parts, bars, pitches, keys and transpositions through MuseScore's own export, its systems, its MuseScore 3 file, its PDF and the optical cross-check."""
from __future__ import annotations

import collections
import collections.abc
import pathlib
import xml.etree.ElementTree as ET
from fractions import Fraction as F

from muse import chordnames, gate, launch, pdfcheck, pdftext, port, scorefile, verify
from muse.model import Orchestra
from muse.song import Song, orchestra_of


def file_bars(xml: pathlib.Path) -> list[tuple[str, ET.Element]]:
    root = ET.parse(xml).getroot()
    names = {part.get("id"): part.findtext("part-name") or "" for part in root.iter("score-part")}
    return [(names.get(part.get("id"), ""), part) for part in root.findall("part")]


def content_problems(orchestra: Orchestra, xml: pathlib.Path) -> list[str]:
    """The score file, through MuseScore's own export, against the decode: parts, bars, written pitches, keys, transpositions and every voice's length."""
    found = file_bars(xml)
    problems = []
    if [name for name, _ in found] != [part.name for part in orchestra.parts]:
        return [f"parts {[name for name, _ in found]} against {[part.name for part in orchestra.parts]}"]
    for (_, element), part in zip(found, orchestra.parts, strict=True):
        measures = element.findall("measure")
        if len(measures) != len(part.bars):
            problems.append(f"{part.name}: {len(measures)} bars against {len(part.bars)}")
            continue
        fifths = [int(k.text or 0) for k in element.iter("fifths")]
        if set(fifths) != {part.key}:
            problems.append(f"{part.name}: keys {sorted(set(fifths))} against {part.key}")
        shift = element.find("measure/attributes/transpose")
        held = None if shift is None else (int(shift.findtext("diatonic") or 0), int(shift.findtext("chromatic") or 0), int(shift.findtext("octave-change") or 0))
        wanted = None if part.transposition is None else (part.transposition.diatonic, part.transposition.chromatic, part.transposition.octave)
        if held != wanted:
            problems.append(f"{part.name}: transposition {held} against {wanted}")
        divisions = int(next(element.iter("divisions")).text or 1)
        for measure, bar in zip(measures, part.bars, strict=True):
            want = sorted(verify.midi(h.step, h.alter, h.octave) for voices in (bar.voices, bar.voices2) for voice in voices for e in voice for h in e.heads)
            got = verify.bar_pitches(measure)
            if got != want:
                problems.append(f"{part.name} bar {bar.number}: pitches {got} against {want}")
            totals: dict[str, int] = collections.Counter()
            for note in measure.findall("note"):
                if note.find("chord") is None and note.find("grace") is None and note.find("duration") is not None:
                    totals[note.findtext("voice") or "1"] += int(note.findtext("duration") or 0)
            for voice, total in totals.items():
                if F(total, divisions) != bar.length:
                    problems.append(f"{part.name} bar {bar.number} voice {voice}: {F(total, divisions)} quarters against {bar.length}")
    return problems


def system_problems(orchestra: Orchestra, mpos: pathlib.Path) -> list[str]:
    """Every system of the score opens on the bar its source system opens on."""
    boxes = [(int(float(e.get("page") or 0)), round(float(e.get("y") or 0))) for e in ET.parse(mpos).getroot().iter("element")]
    starts = [at + 1 for at, box in enumerate(boxes) if not at or box != boxes[at - 1]]
    wanted = [1] + [bar.number for bar in orchestra.parts[0].bars if bar.new_system]
    return [] if starts == wanted else [f"systems open on bars {starts}, the source's on {wanted}"]


def navigation_problems(score_file: pathlib.Path) -> list[str]:
    """The full score reads straight through: the decode reads no repeat or jump, so the file holds none."""
    found = [f"bar {at}: {bar.signs + [str(j) for j in bar.jumps]}" for staff in scorefile.read(score_file) for at, bar in enumerate(staff, 1) if bar.signs or bar.jumps]
    return found[:5]


def homr_problems(src: pathlib.Path, orchestra: Orchestra, work: pathlib.Path) -> tuple[list[str], str]:
    """homr's reading of every page against the decode's note letters, page by page in staff order."""
    command = gate.homr_command()
    if not command:
        return ["homr not found: install it or set MUSE_HOMR"], ""
    ours: list[str] = []
    where: list[int] = []
    theirs: list[str] = []
    for page_no in sorted({bar.page for bar in orchestra.parts[0].bars}):
        for part in orchestra.parts:
            for bar in (b for b in part.bars if b.page == page_no):
                for voices in (bar.voices, bar.voices2):
                    for ev in (e for voice in voices for e in voice if e.heads and not e.grace):
                        ours.append(ev.heads[-1].step)
                        where.append(bar.number)
        theirs += gate.musicxml_letters(gate.homr_reading(src, page_no, work, command))
    share, bars = gate.agreement(ours, where, theirs)
    failures = [f"homr agrees on only {share:.0%} of notes"] if share < gate.MIN_HOMR_AGREEMENT else []
    return failures, f"homr agrees on {share:.0%}; differs near bars {bars[:20]}"


CHECKS = ("verify", "systems", "navigate", "chord file", "ms3", "pdf", "homr")  # every check a full score takes; chord, sign-box, piano, line-layout and even-bar checks do not apply


def problems(src: pathlib.Path, out: pathlib.Path, work: pathlib.Path, info: Song) -> tuple[list[str], list[str]]:
    """Every check that applies to a full score, each speaking even when another crashed."""
    failures: list[str] = []
    notes: list[str] = []

    def guarded(label: str, step: collections.abc.Callable[[], list[str]]) -> None:
        try:
            failures.extend(f"{label}: {problem}" for problem in step())
        except launch.MachineUnavailable:
            raise
        except Exception as error:  # every check must speak even when another crashed
            failures.append(f"{label}: could not run: {str(error)[:160]}")

    orchestra = orchestra_of(src)
    final = out / info.score_file
    pdf = out / info.original_pdf
    exported = work / f"{info.name}-final.musicxml"
    probe = work / f"{info.name}-final.mpos"

    def content() -> list[str]:
        launch.mscore(["-o", str(exported), str(final)], port.LAUNCH_SECONDS, check=True)
        return content_problems(orchestra, exported)

    def systems() -> list[str]:
        launch.mscore(["-o", str(probe), str(final)], port.LAUNCH_SECONDS, check=True)
        return system_problems(orchestra, probe)

    def legacy() -> list[str]:
        held = out / info.legacy_file
        if not held.exists():
            return [f"{held.name} withheld"]
        return scorefile.difference(final, held) + chordnames.chord_path_problems(held)

    def page() -> list[str]:
        notes.append(f"{pdf.name}: {len(pdftext.open(pdf))} pages")
        return gate.page_problems(pdf) + gate.ink_problems(pdf) + verify.pdf_words_problems(pdf, info.title) + pdfcheck.foreign_fonts(pdf) + [f"off the page: {what}" for _, _, what in pdfcheck.off_page(pdf)]

    def homr() -> list[str]:
        wrong, note = homr_problems(src, orchestra, work)
        if note:
            notes.append(note)
        return wrong

    steps = {"verify": content, "systems": systems, "navigate": lambda: navigation_problems(final), "chord file": lambda: chordnames.chord_path_problems(final), "ms3": legacy, "pdf": page, "homr": homr}
    for label in CHECKS:
        guarded(label, steps[label])
    return failures, notes
