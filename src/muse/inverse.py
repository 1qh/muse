"""Transpose a finished score back through MuseScore and compare it with the source reading."""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import xml.etree.ElementTree as ET

from muse.port import TONES, decode
from muse.verify import PC, bar_pitches, bar_roots, midi, stored_chord_names

SOURCE_KEY = {"Cm": -3, "Dm": -1, "Fm": -4, "Em": 1, "Am": 0}


def check(src: pathlib.Path, out: pathlib.Path, work: pathlib.Path) -> list[str]:
    song, key = src.stem.split("-")
    tone, _, _, semis = TONES[song]
    final = out / f"{song}-{tone}.mscz"
    back = work / f"{song}-back.mscz"
    direction = "up" if semis < 0 else "down"
    if semis:
        options = {"mode": "to_key", "direction": direction, "targetKey": SOURCE_KEY[key], "transposeKeySignatures": True, "transposeChordNames": True, "useDoubleSharpsFlats": False, "transposeInterval": 0}
        subprocess.run(["mscore", "-o", str(back), "--transpose", json.dumps(options), str(final)], capture_output=True, check=True, timeout=240)
    else:
        back.write_bytes(final.read_bytes())
    xml = work / f"{song}-back.musicxml"
    subprocess.run(["mscore", "-o", str(xml), str(back)], capture_output=True, check=True, timeout=240)
    root = ET.parse(xml).getroot()
    score = decode(src)
    problems = []
    keys = {k.text for k in root.iter("fifths")}
    if keys != {str(SOURCE_KEY[key])}:
        problems.append(f"key {keys}")
    for p_i, part in enumerate(root.findall("part")):
        for m, bar in zip(part.findall("measure"), score.bars, strict=False):
            voices = bar.voices if p_i == 0 else bar.voices2
            want = sorted(midi(h.step, h.alter, h.octave) for v in voices for e in v for h in e.heads)
            got = bar_pitches(m)
            if want != got:
                problems.append(f"bar {bar.number} part {p_i + 1}: {got} vs {want}")
            if p_i == 0:
                want_c = sorted(((PC[c.root] + c.root_alter) % 12, -1 if not c.bass else (PC[c.bass] + c.bass_alter) % 12) for c in bar.chords)
                if want_c != bar_roots(m):
                    problems.append(f"bar {bar.number}: chords {bar_roots(m)} vs {want_c}")
    if stored_chord_names(back) != sorted(c.kind for b in score.bars for c in b.chords if c.kind):
        problems.append("stored chord names differ from the source")
    return problems


def main() -> None:
    src_dir, out, work = map(pathlib.Path, sys.argv[1:4])
    bad = 0
    for src in sorted(src_dir.glob("*.pdf")):
        problems = check(src, out, work)
        print(src.stem, "problems", len(problems))
        for p in problems[:12]:
            print("  ", str(p)[:170])
        bad += len(problems)
    raise SystemExit(1 if bad else 0)


if __name__ == "__main__":
    main()
