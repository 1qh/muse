"""Transpose the original-tone score through MuseScore, as the favourite-tone sheets are, and compare it with the source reading."""
from __future__ import annotations

import json
import pathlib
import sys
import xml.etree.ElementTree as ET

from muse import chordnames, launch
from muse.song import decode, song_of, sources
from muse.verify import PC, bar_pitches, bar_roots, expected_keys, midi, same_keys, stored_chord_names


def check(src: pathlib.Path, out: pathlib.Path, work: pathlib.Path) -> list[str]:
    info = song_of(src)
    song, semis, fifths = info.name, info.semis, info.fifths
    final = out / info.score_file
    moved = work / f"{song}-moved.mscz"
    if semis:
        options = {"mode": "to_key", "direction": info.direction, "targetKey": fifths, "transposeKeySignatures": True, "transposeChordNames": True, "useDoubleSharpsFlats": False, "transposeInterval": 0}
        launch.mscore(["-o", str(moved), "--transpose", json.dumps(options), str(final)], 240, check=True)
    else:
        moved.write_bytes(final.read_bytes())
    xml = work / f"{song}-moved.musicxml"
    launch.mscore(["-o", str(xml), str(moved)], 240, check=True)
    root = ET.parse(xml).getroot()
    score = decode(src)
    problems = []
    keys = {k.text for k in root.iter("fifths")}
    if not same_keys(keys, expected_keys(score, fifths)):
        problems.append(f"key {keys}")
    for p_i, part in enumerate(root.findall("part")):
        for m, bar in zip(part.findall("measure"), score.bars, strict=False):
            voices = bar.voices if p_i == 0 else bar.voices2
            want = sorted(midi(h.step, h.alter, h.octave) + semis for v in voices for e in v for h in e.heads)
            got = bar_pitches(m)
            if want != got:
                problems.append(f"bar {bar.number} part {p_i + 1}: {got} vs {want}")
            if p_i == 0:
                want_c = sorted(((PC[c.root] + c.root_alter + semis) % 12, -1 if not c.bass else (PC[c.bass] + c.bass_alter + semis) % 12) for c in bar.chords)
                if want_c != bar_roots(m):
                    problems.append(f"bar {bar.number}: chords {bar_roots(m)} vs {want_c}")
    if stored_chord_names(moved) != sorted(chordnames.stored(c.kind) for b in score.bars for c in b.chords if c.kind):
        problems.append("stored chord names differ from the source")
    return problems


def main() -> None:
    src_dir, out, work = map(pathlib.Path, sys.argv[1:4])
    bad = 0
    for src in sources(src_dir):
        problems = check(src, out, work)
        print(src.stem, "problems", len(problems))
        for p in problems[:12]:
            print("  ", str(p)[:170])
        bad += len(problems)
    raise SystemExit(1 if bad else 0)


if __name__ == "__main__":
    main()
