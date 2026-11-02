"""Print each bar's reading and every printed bar number check."""
from __future__ import annotations

import sys
from fractions import Fraction as F

from muse.symbolfont import decode

score = decode(sys.argv[1])
for bar in score.bars:
    rows = []
    for name, voices in (("A", bar.voices), ("B", bar.voices2)):
        for voice in voices:
            total = sum((e.length for e in voice), F(0))
            ok = "" if total == bar.length else f"!{total}"
            rows.append(name + ok + " " + " ".join(("+".join(h.step + {-1: "b", 1: "#", 0: ""}[h.alter] + str(h.octave) for h in e.heads) or ("H" if e.hidden else "r")) + ":" + str(e.length) for e in voice))
    chords = " ".join(c.root + {-1: "b", 1: "#", 0: ""}[c.root_alter] + c.kind + ("/" + c.bass if c.bass else "") for c in bar.chords)
    print(f"{bar.number} p{bar.page} len={bar.length} [{chords}] " + " || ".join(rows))
for f in score.flags:
    print("FLAG", f.bar, f.message)
print("bars", len(score.bars), "flags", len(score.flags))
