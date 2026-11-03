"""Port 1 source PDF: decode, write MusicXML, create the MuseScore file, transpose, export."""
from __future__ import annotations

import concurrent.futures
import itertools
import json
import pathlib
import re
import subprocess
import sys
import zipfile
from collections.abc import Callable

import pymupdf

from muse import bravura, musicxml, pdfcheck, symbolfont
from muse.model import Bar, Score

MAX_LAYOUT_ROUNDS = 40
MAX_MERGE_ROUNDS = 12
BARS_PER_LINE = 8
TIE_REACH = 2  # extra bars a line may take to avoid breaking on a tie
PICKUP_EVENTS = 2  # a first bar this thin shares the opening line as a pickup
MIN_SPATIUM = 1.2
MAX_PAGES = 2
MAX_SHORT_SHARE = 0.15  # readers find many 2-bar lines sparse; smaller notation is preferred
CHORD_STRETCH = 0.45
TIGHT_DISTANCE = 6.0  # line gap only for a song that fits its pages no other way
BASE_DISTANCE = 11.0
MAX_SPATIUM = 3.0
MAX_DISTANCE = 40.0
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


TITLES = {
    "SongA": "Song A",
    "SongB": "Song B",
    "SongC": "Song C",
    "SongD": "Song D",
    "SongE": "Song E",
    "SongF": "Song F",
    "SongG": "Song G",
    "SongH": "Song H",
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


def crowded_rows(pdf: pathlib.Path, firsts: list[int]) -> set[int]:
    """First bar of every line holding chords that run together, matched by page and height."""
    where = []
    for page_no, page in enumerate(pymupdf.open(pdf)):
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    if span["text"].strip().isdigit() and span["bbox"][0] < 60 and "Italic" in span["font"] and int(span["text"]) in firsts:
                        where.append((page_no, span["bbox"][1], int(span["text"])))
    where.append((0, 0.0, 1))
    crowded = set()
    for page_no, y, _ in pdfcheck.chord_collisions(pdf):
        same_page = [item for item in where if item[0] == page_no]
        crowded.add(min(same_page, key=lambda item: abs(item[1] - y))[2])
    for page_no, y, _ in pdfcheck.past_margin(pdf):
        above = [item for item in where if item[0] == page_no and item[1] <= y]
        same_page = above or [item for item in where if item[0] == page_no]
        crowded.add(max(same_page, key=lambda item: item[1])[2])
    return crowded


def must_start_line(bar: Bar, previous: Bar | None) -> bool:
    """Only a section mark starts a new block of lines."""
    return previous is None or bool(bar.rehearsal or bar.segno or bar.coda or previous.final)


def sections(score: Score) -> list[list[int]]:
    """Bar indexes of each block; a tie into a section mark keeps the bars together."""
    blocks: list[list[int]] = []
    for index, bar in enumerate(score.bars):
        previous = score.bars[index - 1] if index else None
        if must_start_line(bar, previous) and (previous is None or not tied_out(previous)):
            blocks.append([])
        blocks[-1].append(index)
    return blocks


def grid(count: int, per_line: int, tied: list[bool], pickup: bool = False) -> list[int]:
    """Line starts at a steady bars-per-line: no single bar left over, and no break on a tie when 1 or 2 more bars avoid it."""
    starts = []
    at = 0
    while at < count:
        starts.append(at)
        end = at + per_line + (1 if pickup and at == 0 else 0)
        if count - end == 1 and per_line > 1:
            end = count
        limit = at + per_line + TIE_REACH
        while end < count and tied[end - 1] and end < limit:
            end += 1
        if end < count and tied[end - 1]:
            back = at + per_line
            while back > at + 1 and tied[back - 1]:
                back -= 1
            end = back if not tied[back - 1] else at + per_line
        at = end
    return starts


def opening_lines(score: Score) -> list[int]:
    """0-based bars that start a line before any line has been judged too crowded."""
    starts = []
    for number, block in enumerate(sections(score)):
        tied = [tied_out(score.bars[i]) for i in block]
        first_events = sum(len(voice) for voice in score.bars[block[0]].voices)
        pickup = number == 0 and first_events <= PICKUP_EVENTS
        starts += [block[start] for start in grid(len(block), BARS_PER_LINE, tied, pickup)]
    return starts


def short_line_share(lines: list[list[int]]) -> float:
    """Share of lines with 2 bars or fewer."""
    return sum(1 for line in lines if len(line) <= 2) / len(lines)


def merge_candidates(lines: list[list[int]], fixed: set[int], refused: set[int]) -> set[int]:
    """0-based line starts to try removing: shortest neighbours first, each line in at most 1 merge."""
    pairs = sorted(range(len(lines) - 1), key=lambda n: len(lines[n]) + len(lines[n + 1]))
    used: set[int] = set()
    joins = set()
    for n in pairs:
        start = lines[n + 1][0] - 1
        if n in used or n + 1 in used or start in fixed or start in refused:
            continue
        if len(lines[n]) + len(lines[n + 1]) <= BARS_PER_LINE:
            joins.add(start)
            used |= {n, n + 1}
    return joins


def split_at(first: int, end: int, tied: list[bool]) -> int:
    """Where to halve line [first, end): no stranded single bar, then off a tie, then nearest the middle."""
    middle = (first + end) // 2

    def cost(at: int) -> tuple[bool, bool, int, int]:
        lone = end - first > 2 and (at == first + 1 or at == end - 1)
        return (lone, tied[at - 1], abs(at - middle), at)

    return min(range(first + 1, end), key=cost)


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


class DoesNotFit(Exception):
    """This size does not fit the song's page count."""


def styled(work: pathlib.Path, title: str, spatium: float, distance: float) -> pathlib.Path:
    """A copy of the style at 1 notation size and line gap, with the song title at the top right of every page."""
    text = STYLE.read_text(encoding="utf-8")
    for tag, value in (("Spatium", spatium), ("minSystemDistance", distance), ("maxSystemDistance", distance), ("oddHeaderR", title), ("evenHeaderR", title)):
        text, count = re.subn(rf"<{tag}>[^<]*</{tag}>", f"<{tag}>{value}</{tag}>", text)
        if count != 1:
            raise RuntimeError(f"style has no single {tag}")
    path = work / f"{title}.mss"
    path.write_text(text, encoding="utf-8")
    return path


def chord_stretch(chords: int) -> float:
    """Width share of a bar: every chord past the first asks for room inside the line."""
    return 1.0 + CHORD_STRETCH * max(0, chords - 1)


def widen_chord_bars(mscz: pathlib.Path, score: Score) -> None:
    """Give bars with more chords more width, so a line rarely has to split for them."""
    with zipfile.ZipFile(mscz) as z:
        items = {info.filename: z.read(info.filename) for info in z.infolist()}
    name = next(n for n in items if n.endswith(".mscx") and "/" not in n)
    text = items[name].decode("utf-8")
    first = text.index('<Staff id="1">', text.index("</Part>"))
    end = text.find('<Staff id="2">', first)
    end = end if end != -1 else text.index("</Score>", first)
    body = text[first:end]
    numbers = iter(score.bars)

    def widen(match: re.Match[str]) -> str:
        bar = next(numbers, None)
        factor = chord_stretch(len(bar.chords)) if bar else 1.0
        return match.group(0) + f"<stretch>{factor:.2f}</stretch>" if factor > 1.0 else match.group(0)

    body = re.sub(r"<Measure[^>]*>", widen, body)
    items[name] = (text[:first] + body + text[end:]).encode("utf-8")
    with zipfile.ZipFile(mscz, "w", zipfile.ZIP_DEFLATED) as z:
        for key, data in items.items():
            z.writestr(key, data)


def transpose_options(song: str) -> dict[str, object] | None:
    _, fifths, direction, _ = TONES[song]
    if not direction:
        return None
    return {"mode": "to_key", "direction": direction, "targetKey": fifths, "transposeKeySignatures": True, "transposeChordNames": True, "useDoubleSharpsFlats": False, "transposeInterval": 0}


def export_probe(song: str, base: pathlib.Path, probe: pathlib.Path) -> None:
    """Measure the transposed score: chord names, and so their widths, differ from the source's."""
    options = transpose_options(song)
    if options:
        run("-o", str(probe), "--transpose", json.dumps(options), str(base))
    else:
        run("-o", str(probe), str(base))


def lay_out(src: pathlib.Path, work: pathlib.Path, spatium: float, distance: float, pages: int | None) -> tuple[Score, pathlib.Path, pathlib.Path]:
    """Build 1 song at 1 size: no line wraps on its own and lines spread evenly over its pages."""
    song = src.stem.split("-")[0]
    style = styled(work, TITLES[song], spatium, distance)
    score = decode(src)
    xml = work / f"{song}.musicxml"
    base = work / f"{song}.mscz"
    probe = work / f"{song}-layout.pdf"
    starts = set(opening_lines(score))
    tied = [tied_out(bar) for bar in score.bars]
    fixed = {block[0] for block in sections(score)}

    def measure() -> tuple[list[list[int]], set[int]]:
        for index, bar in enumerate(score.bars):
            bar.new_system = index in starts and index != 0
            bar.new_page = False
        xml.write_text(musicxml.write(score), encoding="utf-8")
        run("-S", str(style), "-o", str(base), str(xml))
        strip_title_frame(base)
        real_jumps(base)
        widen_chord_bars(base, score)
        export_probe(song, base, probe)
        declared = rows(score)
        firsts = {row[0] for row in declared}
        wraps = [n for n in system_starts(probe) if n not in firsts]
        return declared, set(wraps) | crowded_rows(probe, [row[0] for row in declared])

    for _ in range(MAX_LAYOUT_ROUNDS):
        declared, bad = measure()
        if not bad:
            break
        split = False
        for row in declared:
            if any(n in row for n in bad) and len(row) > 1:
                starts.add(split_at(row[0] - 1, row[-1], tied))
                split = True
        if not split:
            raise DoesNotFit(f"{song}: bars {sorted(bad)} do not fit alone")
    else:
        raise DoesNotFit(f"{song}: lines {sorted(bad)} still do not fit")
    refused: set[int] = set()
    for _ in range(MAX_MERGE_ROUNDS):
        joins = merge_candidates(rows(score), fixed, refused)
        if not joins:
            break
        starts -= joins
        declared, bad = measure()
        undone = {at for row in declared if any(n in row for n in bad) for at in joins if row[0] - 1 < at <= row[-1] - 1}
        refused |= undone
        starts |= undone
    measure()
    found = len(pymupdf.open(probe))
    if pages is not None and found > pages:
        raise DoesNotFit(f"{song}: {found} pages, over {pages}")
    if balance(score, probe):
        xml.write_text(musicxml.write(score), encoding="utf-8")
        run("-S", str(style), "-o", str(base), str(xml))
        strip_title_frame(base)
        real_jumps(base)
        widen_chord_bars(base, score)
        export_probe(song, base, probe)
        if len(pymupdf.open(probe)) != found:
            raise DoesNotFit(f"{song}: even pages need {len(pymupdf.open(probe))} pages, not {found}")
    return score, base, probe


def largest_fit(start: float, step: float, stop: float, fits: Callable[[float], bool]) -> float:
    """Bisect for the largest value that fits, to within 1 step; start is known to fit."""
    low, high = start, stop + step
    while high - low > step + 1e-9:
        middle = round(start + round(((low + high) / 2 - start) / step) * step, 2)
        if fits(middle):
            low = middle
        else:
            high = middle
    return low


def port(src: pathlib.Path, out_dir: pathlib.Path, work: pathlib.Path) -> Score:
    song = src.stem.split("-")[0]
    tone = TONES[song][0]
    work.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    pages = MAX_PAGES

    def fits(spatium: float, distance: float, compact: bool = True) -> bool:
        try:
            laid, _, _ = lay_out(src, work, spatium, distance, pages)
        except DoesNotFit:
            return False
        return not compact or short_line_share(rows(laid)) <= MAX_SHORT_SHARE

    start = next((gap for gap in (BASE_DISTANCE, TIGHT_DISTANCE) if fits(MIN_SPATIUM, gap, compact=False)), None)
    if start is None:
        raise DoesNotFit(f"{song}: over {pages} pages even at the smallest size")
    spatium = largest_fit(MIN_SPATIUM, 0.05, MAX_SPATIUM, lambda value: fits(value, start))
    distance = largest_fit(start, 1.0, MAX_DISTANCE, lambda value: fits(spatium, value, compact=False))
    score, base, _ = lay_out(src, work, spatium, distance, pages)
    print(song, "pages", pages, "spatium", spatium, "distance", distance, flush=True)
    final = out_dir / f"{song}-{tone}.mscz"
    options = transpose_options(song)
    if options:
        run("-o", str(final), "--transpose", json.dumps(options), str(base))
    else:
        final.write_bytes(base.read_bytes())
    run("-o", str(final.with_suffix(".pdf")), str(final))
    return score


def main() -> None:
    src_dir, out_dir, work = map(pathlib.Path, sys.argv[1:4])
    wanted = sys.argv[4:]
    sources = [src for src in sorted(src_dir.glob("*.pdf")) if not wanted or src.stem.split("-")[0] in wanted]
    with concurrent.futures.ProcessPoolExecutor(max_workers=len(sources)) as pool:
        results = pool.map(port, sources, itertools.repeat(out_dir), itertools.repeat(work))
        for src, score in zip(sources, results, strict=True):
            print(src.stem, "bars", len(score.bars), "flags", len(score.flags), "corrections", len(score.corrections))
            for issue in score.flags:
                print("  FLAG", issue.bar, issue.message)


if __name__ == "__main__":
    main()
