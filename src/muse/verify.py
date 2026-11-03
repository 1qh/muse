"""Check a finished song against its source decode, through MuseScore's own export of the final file."""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile
from fractions import Fraction as F

from muse.pdfcheck import title_problems
from muse.song import decode, song_of

BANNED = ["ENGRAVER", "Copyright", "Contact", "credit", "Sáng tác", "Trình bày", "credit", "credit"]
PC = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}


def midi(step: str, alter: int, octave: int) -> int:
    return 12 * (octave + 1) + PC[step] + alter


def text_of(node: ET.Element, tag: str) -> str:
    return node.findtext(tag) or ""


def alter_of(node: ET.Element, tag: str) -> int:
    return int(float(node.findtext(tag) or 0))


def bar_pitches(m: ET.Element) -> list[int]:
    got = []
    for n in m.findall("note"):
        pitch = n.find("pitch")
        if pitch is not None:
            got.append(midi(text_of(pitch, "step"), alter_of(pitch, "alter"), int(text_of(pitch, "octave"))))
    return sorted(got)


def bar_roots(m: ET.Element) -> list[tuple[int, int]]:
    got = []
    for h in m.findall("harmony"):
        r, b = h.find("root"), h.find("bass")
        assert r is not None
        root = (PC[text_of(r, "root-step")] + alter_of(r, "root-alter")) % 12
        got.append((root, -1 if b is None else (PC[text_of(b, "bass-step")] + alter_of(b, "bass-alter")) % 12))
    return sorted(got)


def stored_chord_names(final: pathlib.Path) -> list[str]:
    with zipfile.ZipFile(final) as z:
        text = z.read(next(n for n in z.namelist() if n.endswith(".mscx"))).decode()
    return sorted(re.findall(r"<Harmony>(?:(?!</Harmony>).)*?<name>(.*?)</name>", text, re.S))


def check(src: pathlib.Path, out: pathlib.Path, work: pathlib.Path) -> list[str]:
    info = song_of(src)
    song, tone, fifths, semis = info.name, info.tone, info.fifths, info.semis
    final = out / f"{song}-{tone}.mscz"
    score = decode(src)
    xml = work / f"{song}-final.musicxml"
    subprocess.run(["mscore", "-o", str(xml), str(final)], capture_output=True, check=True, timeout=240)
    root = ET.parse(xml).getroot()
    problems = []
    parts = root.findall("part")
    counts = [len(p.findall("measure")) for p in parts]
    if any(c != len(score.bars) for c in counts):
        problems.append(f"bar count {counts} vs {len(score.bars)}")
    keys = {k.text for k in root.iter("fifths")}
    if keys != {str(fifths)}:
        problems.append(f"key {keys}")
    notes = chords = 0
    for p_i, part in enumerate(parts):
        for m, bar in zip(part.findall("measure"), score.bars, strict=False):
            voices = bar.voices if p_i == 0 else bar.voices2
            want = sorted(midi(h.step, h.alter, h.octave) + semis for v in voices for e in v for h in e.heads)
            got = bar_pitches(m)
            notes += len(got)
            if got != want:
                problems.append(f"bar {bar.number} part {p_i + 1}: pitches {got} expected {want}")
            if p_i == 0:
                want_c = sorted(((PC[c.root] + c.root_alter + semis) % 12, -1 if not c.bass else (PC[c.bass] + c.bass_alter + semis) % 12) for c in bar.chords)
                got_c = bar_roots(m)
                chords += len(got_c)
                if want_c != got_c:
                    problems.append(f"bar {bar.number}: chords {got_c} expected {want_c}")
    div = int(next(root.iter("divisions")).text or 0)
    for m, bar in zip(parts[0].findall("measure"), score.bars, strict=False):
        totals: dict[str, int] = {}
        for n in m.findall("note"):
            if n.find("chord") is None and n.find("grace") is None and n.find("duration") is not None:
                v = n.findtext("voice") or "1"
                totals[v] = totals.get(v, 0) + int(text_of(n, "duration"))
        for v, t in totals.items():
            if F(t, div) != bar.length:
                problems.append(f"bar {bar.number} voice {v}: {F(t, div)} quarters vs {bar.length}")
    text = subprocess.run(["pdftotext", str(final.with_suffix(".pdf")), "-"], capture_output=True, text=True, check=True).stdout
    for word in BANNED:
        if word.lower() in text.lower():
            problems.append(f"banned text in PDF: {word}")
    if re.search(r"\d{9,}", text):
        problems.append("long digit run in PDF")
    problems += title_problems(final.with_suffix(".pdf"), info.title)
    if stored_chord_names(final) != sorted(c.kind for bar in score.bars for c in bar.chords if c.kind):
        problems.append("stored chord names differ from the source")
    print(f"{song}-{tone}: bars {len(score.bars)} notes {notes} chords {chords} corrections {len(score.corrections)} flags {len(score.flags)} problems {len(problems)}")
    return problems


def main() -> None:
    src_dir, out, work = map(pathlib.Path, sys.argv[1:4])
    total = 0
    for src in sorted(src_dir.glob("*.pdf")):
        for p in check(src, out, work):
            print("  ", p)
            total += 1
    print("problems", total)
    raise SystemExit(1 if total else 0)


if __name__ == "__main__":
    main()
