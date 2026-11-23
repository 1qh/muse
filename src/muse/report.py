"""Print each bar's reading and every printed bar number check."""

from __future__ import annotations

import pathlib
import sys
from fractions import Fraction

from muse.song import decode

score = decode(pathlib.Path(sys.argv[1]))
for bar in score.bars:
    rows = []
    for name, voices in (("A", bar.voices), ("B", bar.voices2)):
        for voice in voices:
            total = sum((e.length for e in voice), Fraction(0))
            ok = "" if total == bar.length else f"!{total}"
            rows.append(
                name
                + ok
                + " "
                + " ".join(
                    (
                        "+".join(h.step + {-1: "b", 1: "#", 0: ""}[h.alter] + str(h.octave) for h in e.heads)
                        or ("H" if e.hidden else "r")
                    )
                    + ":"
                    + str(e.length)
                    for e in voice
                ),
            )
    chords = " ".join(
        c.root + {-1: "b", 1: "#", 0: ""}[c.root_alter] + c.kind + ("/" + c.bass if c.bass not in {None, ""} else "")
        for c in bar.chords
    )
    sys.stdout.write(f"{bar.number} p{bar.page} len={bar.length} [{chords}] " + " || ".join(rows) + "\n")
for f in score.flags:
    sys.stdout.write(f"FLAG {f.bar} {f.message}\n")
sys.stdout.write(f"bars {len(score.bars)} flags {len(score.flags)}\n")
