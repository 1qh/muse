"""Check a finished song against its source decode, through MuseScore's own export of the final file."""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile
from fractions import Fraction as F

from muse.port import TONES, decode

BANNED = ["ENGRAVER", "Copyright", "Contact", "credit", "Sáng tác", "Trình bày", "credit", "credit"]
PC = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
TITLES = {"SongA": "SONG A", "SongB": "SONG B", "SongC": "SONG C", "SongD": "SONG D", "SongE": "SONG E", "SongF": "SONG F", "SongG": "Song G", "SongH": "SONG H"}


def midi(step: str, alter: int, octave: int) -> int:
    return 12 * (octave + 1) + PC[step] + alter


def check(src: pathlib.Path, out: pathlib.Path, work: pathlib.Path) -> list[str]:
    song = src.stem.split("-")[0]
    tone, fifths, _, semis = TONES[song]
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
        for m, bar in zip(part.findall("measure"), score.bars):
            source_voices = bar.voices if p_i == 0 else bar.voices2
            want = sorted(midi(h.step, h.alter, h.octave) + semis for v in source_voices for e in v for h in e.heads)
            got = []
            per_voice: dict[str, F] = {}
            for n in m.findall("note"):
                d = n.find("duration")
                if d is not None and n.find("chord") is None and n.find("grace") is None:
                    v = n.findtext("voice") or "1"
                    per_voice[v] = per_voice.get(v, F(0)) + int(d.text)
                pitch = n.find("pitch")
                if pitch is not None:
                    got.append(midi(pitch.findtext("step"), int(float(pitch.findtext("alter") or 0)), int(pitch.findtext("octave"))))
            got.sort()
            notes += len(got)
            if got != want:
                problems.append(f"bar {bar.number} part {p_i + 1}: pitches {got} expected {want}")
            div = None
            bar_len = bar.length
            for v, total in per_voice.items():
                pass
            if p_i == 0:
                want_c = [((PC[c.root] + c.root_alter + semis) % 12, c.kind, None if not c.bass else (PC[c.bass] + c.bass_alter + semis) % 12) for c in bar.chords]
                got_c = []
                for h in m.findall("harmony"):
                    r = h.find("root")
                    b = h.find("bass")
                    got_c.append(((PC[r.findtext("root-step")] + int(float(r.findtext("root-alter") or 0))) % 12, (h.find("kind").get("text") or ""),
                                  None if b is None else (PC[b.findtext("bass-step")] + int(float(b.findtext("bass-alter") or 0))) % 12))
                chords += len(got_c)
                if sorted((x[0], -1 if x[2] is None else x[2]) for x in want_c) != sorted((x[0], -1 if x[2] is None else x[2]) for x in got_c):
                    problems.append(f"bar {bar.number}: chords {got_c} expected {want_c}")
    # bar sums on the exported file
    div = int(next(root.iter("divisions")).text)
    current = None
    part = parts[0]
    for m, bar in zip(part.findall("measure"), score.bars):
        totals: dict[str, int] = {}
        for n in m.findall("note"):
            if n.find("chord") is None and n.find("grace") is None and n.find("duration") is not None:
                v = n.findtext("voice") or "1"
                totals[v] = totals.get(v, 0) + int(n.findtext("duration"))
        for v, t in totals.items():
            if F(t, div) != bar.length:
                problems.append(f"bar {bar.number} voice {v}: {F(t, div)} quarters vs {bar.length}")
    text = subprocess.run(["pdftotext", str(final.with_suffix(".pdf")), "-"], capture_output=True, text=True, check=True).stdout
    for word in BANNED:
        if word.lower() in text.lower():
            problems.append(f"banned text in PDF: {word}")
    if re.search(r"\d{9,}", text):
        problems.append("long digit run in PDF")
    if TITLES[song] in text:
        problems.append(f"title in PDF: {TITLES[song]}")
    names = re.findall(r"<harmonyInfo>\s*<name>(.*?)</name>|<Harmony>(?:(?!</Harmony>).)*?<name>(.*?)</name>", zipfile.ZipFile(final).read(next(n for n in zipfile.ZipFile(final).namelist() if n.endswith(".mscx"))).decode(), re.S)
    got_names = sorted(a or b for a, b in names)
    want_names = sorted(c.kind.replace("&amp;", "&") for bar in score.bars for c in bar.chords if c.kind)
    if got_names != want_names:
        problems.append(f"chord names differ: {sorted(set(want_names) ^ set(got_names))} counts {len(got_names)} vs {len(want_names)}")
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
