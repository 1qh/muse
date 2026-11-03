"""Port 1 source PDF: decode, write MusicXML, create the MuseScore file, transpose, export."""
from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys
import zipfile

import pymupdf

from muse import bravura, musicxml, symbolfont
from muse.model import Bar, Score

MAX_LAYOUT_ROUNDS = 6
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


def strip_title_frame(mscz: pathlib.Path) -> None:
    """Import adds an empty frame for a title the sheet does not carry; it only pushes the music down."""
    with zipfile.ZipFile(mscz) as z:
        items = {info.filename: z.read(info.filename) for info in z.infolist()}
    for name, data in items.items():
        if name.endswith(".mscx"):
            items[name] = re.sub(r"\s*<VBox>(?:(?!</VBox>|<Text>).)*</VBox>", "", data.decode("utf-8"), flags=re.S).encode("utf-8")
    with zipfile.ZipFile(mscz, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in items.items():
            z.writestr(name, data)


def decode(path: pathlib.Path) -> Score:
    return bravura.decode(str(path)) if path.stem.startswith("SongG") else symbolfont.decode(str(path))


def system_starts(pdf: pathlib.Path) -> list[int]:
    """The bar number printed at the start of every line, read from the exported PDF."""
    found = []
    for page in pymupdf.open(pdf):
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    if span["text"].strip().isdigit() and span["bbox"][0] < 60 and "Italic" in span["font"]:
                        found.append(int(span["text"]))
    return found


def rows(score: Score) -> list[list[int]]:
    """1-based bar numbers of each declared row."""
    out: list[list[int]] = []
    for number, bar in enumerate(score.bars, 1):
        if not out or bar.new_system or bar.new_page:
            out.append([])
        out[-1].append(number)
    return out


def tied_out(bar: Bar) -> bool:
    """Whether a tie leaves the bar: only a voice's last event can carry one past the barline."""
    return any(head.tie_start for voice in [*bar.voices, *bar.voices2] if voice for head in voice[-1].heads)


def split_point(row: list[int], score: Score) -> int:
    """The bar nearest the row's middle that does not start with a note tied from the bar before; a lone-bar side loses."""
    middle = len(row) // 2

    def cost(number: int) -> tuple[bool, int]:
        at = row.index(number)
        return (len(row) > 3 and (at == 1 or at == len(row) - 1), abs(at - middle))

    candidates = sorted(row[1:], key=cost)
    for number in candidates:
        if not tied_out(score.bars[number - 2]):
            return number
    return candidates[0]


def balanced_pages(heights: list[float], pages: int) -> list[int]:
    """Indexes of the lines that start pages 2 and up, keeping the tallest page as short as possible."""
    count = len(heights)
    pages = min(pages, count)
    prefix = [0.0]
    for height in heights:
        prefix.append(prefix[-1] + height)
    best = {(0, 0): (0.0, -1)}
    for used in range(1, pages + 1):
        for end in range(used, count + 1):
            options = [(max(best[(used - 1, start)][0], prefix[end] - prefix[start]), start)
                       for start in range(used - 1, end) if (used - 1, start) in best]
            if options:
                best[(used, end)] = min(options)
    starts = []
    end = count
    for used in range(pages, 0, -1):
        end = best[(used, end)][1]
        starts.append(end)
    return sorted(start for start in starts if start > 0)


def line_heights(pdf: pathlib.Path, firsts: list[int]) -> tuple[list[float], int]:
    """Each line's height from where its bar number prints, the last on a page ending at the page's ink."""
    where: dict[int, tuple[int, float]] = {}
    doc = pymupdf.open(pdf)
    bottoms = []
    tops = []
    for page_no, page in enumerate(doc):
        pix = page.get_pixmap(dpi=72, colorspace=pymupdf.csGRAY)
        inked = [y for y in range(pix.height) if min(pix.samples[y * pix.width:(y + 1) * pix.width]) < 128]
        tops.append(float(inked[0]))
        bottoms.append(float(inked[-1]))
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    if span["text"].strip().isdigit() and span["bbox"][0] < 60 and "Italic" in span["font"]:
                        where.setdefault(int(span["text"]), (page_no, span["bbox"][1]))
    where[1] = (0, tops[0])
    heights = []
    for index, first in enumerate(firsts):
        page_no, y = where[first]
        following = where.get(firsts[index + 1]) if index + 1 < len(firsts) else None
        heights.append(following[1] - y if following and following[0] == page_no else bottoms[page_no] - y)
    return heights, len(doc)


def balance(score: Score, probe: pathlib.Path) -> bool:
    """Spread the lines evenly over the pages they need; False when there is only 1 page."""
    firsts = [row[0] for row in rows(score)]
    heights, pages = line_heights(probe, firsts)
    if pages < 2:
        return False
    for start in balanced_pages(heights, pages):
        score.bars[firsts[start] - 1].new_page = True
    return True


def port(src: pathlib.Path, out_dir: pathlib.Path, work: pathlib.Path) -> Score:
    song = src.stem.split("-")[0]
    tone, fifths, direction, _ = TONES[song]
    score = decode(src)
    for bar in score.bars:  # lines stay; pages fill up
        bar.new_system = bar.new_system or bar.new_page
        bar.new_page = False
    work.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    xml = work / f"{song}.musicxml"
    base = work / f"{song}.mscz"
    probe = work / f"{song}-layout.pdf"
    for _ in range(MAX_LAYOUT_ROUNDS):
        xml.write_text(musicxml.write(score), encoding="utf-8")
        run("-S", str(STYLE), "-o", str(base), str(xml))
        strip_title_frame(base)
        run("-o", str(probe), str(base))
        declared = rows(score)
        firsts = {row[0] for row in declared}
        wraps = [n for n in system_starts(probe) if n not in firsts]
        if not wraps:
            break
        for row in declared:
            if any(n in row for n in wraps):
                score.bars[split_point(row, score) - 1].new_system = True
    else:
        raise RuntimeError(f"{song}: rows still wrap at {wraps}")
    pages = len(pymupdf.open(probe))
    if balance(score, probe):
        xml.write_text(musicxml.write(score), encoding="utf-8")
        run("-S", str(STYLE), "-o", str(base), str(xml))
        strip_title_frame(base)
        run("-o", str(probe), str(base))
        if len(pymupdf.open(probe)) != pages:
            raise RuntimeError(f"{song}: even pages need {len(pymupdf.open(probe))} pages, not {pages}")
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
