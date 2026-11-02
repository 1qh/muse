"""Port 1 source PDF: decode, write MusicXML, create the MuseScore file, transpose, export."""
from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys
import zipfile

from muse import bravura, musicxml, symbolfont
from muse.model import Score

STYLE = pathlib.Path(__file__).with_name("style.mss")
# song -> (target key fifths, direction, semitones)
TONES = {
    "SongA": ("Am", 0, "down", -3),
    "SongB": ("Em", 1, "", 0),
    "SongC": ("Em", 1, "up", 2),
    "SongD": ("Am", 0, "", 0),
    "SongE": ("Em", 1, "up", 2),
    "SongF": ("Em", 1, "down", -1),
    "SongG": ("Em", 1, "", 0),
    "SongH": ("Em", 1, "up", 2),
}


def run(*args: str) -> None:
    done = subprocess.run(["mscore", *args], capture_output=True, text=True, timeout=240)
    if done.returncode:
        raise RuntimeError(f"mscore {args}: {done.stderr[-400:]}")


JUMPS = {
    "To Coda": "<Marker><style>repeat_right</style><text>To Coda</text><label>coda</label></Marker>",
    "D.S. al Coda": "<Jump><text>D.S. al Coda</text><jumpTo>segno</jumpTo><playUntil>coda</playUntil><continueAt>codab</continueAt></Jump>",
}


def real_jumps(mscz: pathlib.Path) -> None:
    """Import keeps jump words as staff text; replace them with MuseScore's own jump elements."""
    items = {}
    with zipfile.ZipFile(mscz) as z:
        for info in z.infolist():
            items[info.filename] = z.read(info.filename)
    for name, data in items.items():
        if not name.endswith(".mscx"):
            continue
        text = data.decode("utf-8")
        for words, element in JUMPS.items():
            pattern = re.compile(r"\s*<StaffText>(?:(?!</StaffText>).)*?<text>" + re.escape(words) + r"</text>\s*</StaffText>", re.S)
            while True:
                m = pattern.search(text)
                if not m:
                    break
                start = text.rfind("<voice>", 0, m.start())
                text = text[:start] + element + text[start:m.start()] + text[m.end():]
        for sym, label in (("segno", "segno"), ("coda", "codab")):
            # the printed sign stays as text at the bar start; a hidden marker carries the jump target
            pattern = re.compile(r"\s*<StaffText>(?:(?!</StaffText>).)*?<text><sym>" + sym + r"</sym></text>\s*</StaffText>", re.S)
            for m in reversed(list(pattern.finditer(text))):
                start = text.rfind("<voice>", 0, m.start())
                element = f"<Marker><visible>0</visible><text><sym>{sym}</sym></text><label>{label}</label></Marker>"
                text = text[:start] + element + text[start:]
        items[name] = text.encode("utf-8")
    with zipfile.ZipFile(mscz, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in items.items():
            z.writestr(name, data)


def decode(path: pathlib.Path) -> Score:
    return bravura.decode(str(path)) if path.stem.startswith("SongG") else symbolfont.decode(str(path))


def port(src: pathlib.Path, out_dir: pathlib.Path, work: pathlib.Path) -> Score:
    song = src.stem.split("-")[0]
    tone, fifths, direction, _ = TONES[song]
    score = decode(src)
    work.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    xml = work / f"{song}.musicxml"
    xml.write_text(musicxml.write(score), encoding="utf-8")
    base = work / f"{song}.mscz"
    run("-S", str(STYLE), "-o", str(base), str(xml))
    real_jumps(base)
    final = out_dir / f"{song}-{tone}.mscz"
    if direction:
        options = {"mode": "to_key", "direction": direction, "targetKey": fifths, "transposeKeySignatures": True, "transposeChordNames": True, "useDoubleSharpsFlats": False, "transposeInterval": 0}
        run("-o", str(final), "--transpose", json.dumps(options), str(base))
    else:
        final.write_bytes(base.read_bytes())
    run("-o", str(final.with_suffix(".pdf")), str(final))
    return score


def main() -> None:
    src_dir, out_dir, work = map(pathlib.Path, sys.argv[1:4])
    wanted = sys.argv[4:]
    for src in sorted(src_dir.glob("*.pdf")):
        if wanted and src.stem.split("-")[0] not in wanted:
            continue
        score = port(src, out_dir, work)
        print(src.stem, "bars", len(score.bars), "flags", len(score.flags), "corrections", len(score.corrections))
        for issue in score.flags:
            print("  FLAG", issue.bar, issue.message)


if __name__ == "__main__":
    main()
