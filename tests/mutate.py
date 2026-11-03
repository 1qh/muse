"""Mutation controls: plant each defect in a copy of the source and require the tests to go red."""
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
MUTATIONS = [
    ("symbolfont.py", "steps = round((base - 0.2 - g.y) / step)", "steps = round((base - 0.2 - g.y) / step) + 1", "pitch"),
    ("symbolfont.py", "nominal = nominal * F(3, 2)", "nominal = nominal", "dot"),
    ("symbolfont.py", "0xF0EE: F(2)", "0xF0EE: F(4)", "half rest"),
    ("symbolfont.py", "level = len({round((b.y0 + b.y1) / 2) for b in hits})", "level = 0", "beam"),
    ("symbolfont.py", "(free, up + down)", "(up, down)", "voice split"),
    ("symbolfont.py", 'return Chord(x, m[1], alter[m[2]], m[3], m[4], alter[m[5] or ""])', "return Chord(x, m[1], 0, m[3], m[4], 0)", "chord accidental"),
    ("symbolfont.py", "x < b.right - 8", "x < b.right - 4", "chord bar"),
    ("symbolfont.py", "ev.heads = list({(h.step, h.octave): h for h in ev.heads}.values())", "pass", "doubled head"),
    ("symbolfont.py", "id(max(hits, key=lambda b: b.width))", "id(min(hits, key=lambda b: abs((b.y0 + b.y1) / 2 - tip)))", "beam group"),
    ("bravura.py", "e.tuplet = True", "e.tuplet = False", "triplet"),
    ("bravura.py", '1 if step_name == "F" else 0', "0", "key signature"),
    ("navigate.py", 'if bar.volta == "1" and second_time:', "if False:", "volta order"),
    ("navigate.py", "after_jump = False  # a repeat", "after_jump = True  # a repeat", "coda repeat"),
    ("pdfcheck.py", "y1 > height * BOTTOM_BAND", "y1 > height * 2", "margin band"),
    ("pdfcheck.py", "MIN_CHORD_GAP = 0.75", "MIN_CHORD_GAP = 0.0", "chord gap"),
    ("pdfcheck.py", "if len(hits) != 1:", "if len(hits) > 99:", "title"),
    ("pdfcheck.py", 'if not name.startswith(MUSIC_FONTS + ("SFPro", "SF-Pro", "SF Pro"))', "if False", "font"),
    ("pdfcheck.py", "and not CHORD.fullmatch(text)", "and False", "chord biggest"),
    ("gate.py", "if len(doc) > port.MAX_PAGES:", "if len(doc) > 99:", "page cap"),
    ("gate.py", "abs(page.rect.width - A4[0]) > A4_TOLERANCE", "False", "a4 width"),
    ("gate.py", "rows[0] < EDGE or", "False or", "ink top"),
    ("gate.py", "run >= MIN_TIED_CHAIN", "run >= 0", "tie allowed"),
    ("gate.py", "share < MIN_HOMR_AGREEMENT", "False", "homr floor"),
    ("gate.py", "letters[-1] = step", "letters.append(step)", "homr chord"),
    ("port.py", "<= BARS_PER_LINE:\n            joins.add", "<= 0:\n            joins.add", "merge pass"),
    ("port.py", "if count - end == 1 and per_line > 1:", "if False:", "line grid"),
    ("port.py", "BARS_PER_LINE = 8", "BARS_PER_LINE = 4", "line length"),
    ("port.py", "if missing:", "if False:", "missing font"),
    ("song.py", "if printed != table[root]:", "if False:", "key disagreement"),
    ("song.py", "semis = (PITCH[target_root] - pitch + 6) % 12 - 6", "semis = PITCH[target_root] - pitch", "nearest target"),
    ("song.py", "raise UnknownEncoding(", "return 'symbolfont' or (", "unknown encoding"),
    ("song.py", "text.title() if text.isupper() else text", "text", "title case"),
    ("symbolfont.py", "if ord(ch[\"c\"]) not in KNOWN_GLYPHS:", "if False:", "unknown glyph"),
    ("publish.py", 'if visibility(out, run) != "PRIVATE":\n        raise Refused(f"{out} is not private', 'if False:\n        raise Refused(f"{out} is not private', "private only"),
    ("publish.py", 'raise Refused(f"gate failed, nothing published', 'print(f"gate failed, nothing published', "gate refusal"),
    ("verify.py", "12 * (octave + 1)", "12 * octave", "midi"),
    ("musicxml.py", "DIV = 48", "DIV = 24", "duration"),
    ("musicxml.py", 'return "major" if not kind else "other"', 'return "major"', "chord kind"),
]


def run(src: pathlib.Path) -> int:
    env = dict(os.environ, PYTHONPATH=str(src))
    args = [sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", "-o", f"pythonpath={src}", str(ROOT / "tests")]
    return subprocess.run(args, env=env, cwd=ROOT, capture_output=True, timeout=300).returncode


def main() -> None:
    survived = 0
    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp) / "src"
        shutil.copytree(ROOT / "src", base, ignore=shutil.ignore_patterns("*.egg-info", "__pycache__"))
        if run(base) != 0:
            raise SystemExit("baseline is red")
        for name, old, new, label in MUTATIONS:
            path = base / "muse" / name
            text = path.read_text()
            if old not in text:
                print(f"{label}: ANCHOR MISSING")
                survived += 1
                continue
            path.write_text(text.replace(old, new))
            code = run(base)
            path.write_text(text)
            print(f"{label}: {'red' if code else 'SURVIVED'}")
            survived += code == 0
    raise SystemExit(1 if survived else 0)


main()
