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
    ("bravura.py", "e.tuplet = True", "e.tuplet = False", "triplet"),
    ("bravura.py", '1 if step_name == "F" else 0', "0", "key signature"),
    ("navigate.py", 'if bar.volta == "1" and second_time:', "if False:", "volta order"),
    ("navigate.py", "after_jump = False  # a repeat", "after_jump = True  # a repeat", "coda repeat"),
    ("pdfcheck.py", "BAND = 0.03", "BAND = 0.0", "margin band"),
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
                raise SystemExit(f"anchor missing: {label}")
            path.write_text(text.replace(old, new))
            code = run(base)
            path.write_text(text)
            print(f"{label}: {'red' if code else 'SURVIVED'}")
            survived += code == 0
    raise SystemExit(1 if survived else 0)


main()
