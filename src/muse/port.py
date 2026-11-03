"""Port 1 source PDF: decode, write MusicXML, create the MuseScore file, transpose, export."""
from __future__ import annotations

import concurrent.futures
import itertools
import json
import os
import pathlib
import re
import subprocess
import sys
import zipfile
from collections.abc import Callable

import pymupdf

from muse import musicxml, pdfcheck, piano, scorefile
from muse.model import Bar, Score
from muse.song import Song, decode, song_of
from muse.song import sources as song_sources

MAX_LAYOUT_ROUNDS = 40
MAX_MERGE_ROUNDS = 12
BARS_PER_LINE = 8
TIE_REACH = 2  # extra bars a line may take to avoid breaking on a tie
PICKUP_EVENTS = 2  # a first bar this thin shares the opening line as a pickup
SIZE_FLOORS = (1.2, 1.1, 1.0)  # below 1.2 only for a song that fits its pages no other way
MAX_PAGES = 2
MAX_WORKERS = 9
SIGN_SIZE = 25  # a sign may exceed the chords: about 2.5 times the default sign
MARKER_TYPES = {"segno": "segno", "coda": "codab"}
LEGACY_SIGN_SIZE = 25  # MuseScore 3 sizes the sign as text
LEGACY_ONLY_FONTS = ("staffFontFace", "systemFontFace", "beginFontFace", "continueFontFace", "endFontFace")  # its names for text styles
JUMP_LIFT = -7  # clears the chord row it would otherwise touch
JUMP_WORD_SIZE = 12  # below the chords
MAX_SHORT_SHARE = 0.15  # readers find many 2-bar lines sparse; smaller notation is preferred
CHORD_STRETCH = 0.45
TIGHT_DISTANCE = 6.0  # line gap only for a song that fits its pages no other way
BASE_DISTANCE = 11.0
MAX_SPATIUM = 3.0
MAX_DISTANCE = 40.0
ORIGINAL = "original"
TONE = "tone"
PIANO = "piano"
STYLE = pathlib.Path(__file__).with_name("style.mss")
SF_PRO_FILES = tuple(f"SF-Pro-Display-{face}.otf" for face in ("Regular", "RegularItalic", "Bold", "BoldItalic"))
FONT_DIRS = (pathlib.Path.home() / "Library" / "Fonts", pathlib.Path("/Library/Fonts"))


class MissingFont(RuntimeError):
    """MuseScore would quietly fall back to another font."""


def require_font(folders: tuple[pathlib.Path, ...] = FONT_DIRS) -> None:
    missing = [name for name in SF_PRO_FILES if not any((folder / name).exists() for folder in folders)]
    if missing:
        raise MissingFont(f"SF Pro Display is missing {missing}; install it from Apple into {folders[0]}")


def run(*args: str) -> None:
    done = subprocess.run(["mscore", *args], capture_output=True, text=True, timeout=240)
    if done.returncode:
        raise RuntimeError(f"mscore {args}: {done.stderr[-400:]}")


JUMPS = {
    "To Coda": "<Marker><style>repeat_right</style><text>To Coda</text><label>coda</label><markerType>coda</markerType></Marker>",
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
            # a visible marker is both the printed sign and the jump target; the style sizes it
            pattern = re.compile(r"\s*<StaffText>(?:(?!</StaffText>).)*?<text><sym>" + sym + r"</sym></text>\s*</StaffText>", re.S)
            for m in reversed(list(pattern.finditer(text))):
                start = text.rfind("<voice>", 0, m.start())
                element = f"<Marker><style>repeat_left</style><text><sym>{sym}</sym></text><label>{label}</label><markerType>{MARKER_TYPES[sym]}</markerType><markerCenterOnSymbol>0</markerCenterOnSymbol><musicSymbolSize>{SIGN_SIZE}</musicSymbolSize></Marker>"
                text = text[:start] + element + text[start:m.start()] + text[m.end():]
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
    for page_no, y, _ in [*pdfcheck.past_margin(pdf), *pdfcheck.text_collisions(pdf)]:
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


def styled(work: pathlib.Path, name: str, title: str, spatium: float, distance: float) -> pathlib.Path:
    """A copy of the style at 1 notation size and line gap, with the song title at the top right of every page."""
    text = STYLE.read_text(encoding="utf-8")
    for tag, value in (("Spatium", spatium), ("minSystemDistance", distance), ("maxSystemDistance", distance), ("oddHeaderR", title), ("evenHeaderR", title)):
        text, count = re.subn(rf"<{tag}>[^<]*</{tag}>", f"<{tag}>{value}</{tag}>", text)
        if count != 1:
            raise RuntimeError(f"style has no single {tag}")
    path = work / name
    path.write_text(text, encoding="utf-8")
    return path


def chord_stretch(chords: int) -> float:
    """Width share of a bar: every chord past the first asks for room inside the line."""
    return 1.0 + CHORD_STRETCH * max(0, chords - 1)


def big_signs(mscz: pathlib.Path) -> None:
    """The jump words must catch the eye in bold; the style sizes the signs."""
    with zipfile.ZipFile(mscz) as z:
        items = {info.filename: z.read(info.filename) for info in z.infolist()}
    for name, data in items.items():
        if name.endswith(".mscx"):
            text = data.decode("utf-8")
            for words in ("To Coda", "D.S. al Coda"):
                text = text.replace(f"<text>{words}</text>", f'<offset x="0" y="{JUMP_LIFT}"/><text><font size="{JUMP_WORD_SIZE}"/><b>{words}</b></text>')
            items[name] = text.encode("utf-8")
    with zipfile.ZipFile(mscz, "w", zipfile.ZIP_DEFLATED) as z:
        for key, data in items.items():
            z.writestr(key, data)


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


def transpose_options(song: Song) -> dict[str, object] | None:
    if not song.direction:
        return None
    return {"mode": "to_key", "direction": song.direction, "targetKey": song.fifths, "transposeKeySignatures": True, "transposeChordNames": True, "useDoubleSharpsFlats": False, "transposeInterval": 0}


def export_view(options: dict[str, object] | None, base: pathlib.Path, target: pathlib.Path) -> None:
    """Export a score, in the sheet's tone when it has its own: chord names, and so their widths, differ by key."""
    if options:
        run("-o", str(target), "--transpose", json.dumps(options), str(base))
    else:
        run("-o", str(target), str(base))


def lay_out(src: pathlib.Path, work: pathlib.Path, spatium: float, distance: float, pages: int | None, variant: str = TONE) -> tuple[Score, pathlib.Path, pathlib.Path]:
    """Build 1 sheet of a song at 1 size: no line wraps on its own and lines spread evenly over its pages."""
    score = decode(src)
    info = song_of(src, score)
    if variant == PIANO:
        piano.reduce(score)
    options = None if variant == ORIGINAL else transpose_options(info)
    song = f"{info.name}-{variant}"
    style = styled(work, f"{song}.mss", info.title, spatium, distance)
    xml = work / f"{song}.musicxml"
    base = work / f"{song}.mscz"
    probe = work / f"{song}-layout.pdf"
    starts = set(opening_lines(score))
    tied = [tied_out(bar) for bar in score.bars]
    fixed = {block[0] for block in sections(score)}

    def build() -> None:
        xml.write_text(musicxml.write(score), encoding="utf-8")
        run("-S", str(style), "-o", str(base), str(xml))
        strip_title_frame(base)
        real_jumps(base)
        big_signs(base)
        widen_chord_bars(base, score)
        export_view(options, base, probe)

    def measure() -> tuple[list[list[int]], set[int]]:
        for index, bar in enumerate(score.bars):
            bar.new_system = index in starts and index != 0
            bar.new_page = False
        build()
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
        build()
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


def fit(src: pathlib.Path, work: pathlib.Path, variant: str, limit: int) -> tuple[Score, pathlib.Path, float, float, int] | None:
    """The largest notation of 1 sheet within a page limit, its lines then spread over the height left."""

    def fits(spatium: float, distance: float, compact: bool = True) -> bool:
        try:
            laid, _, _ = lay_out(src, work, spatium, distance, limit, variant)
        except DoesNotFit:
            return False
        return not compact or short_line_share(rows(laid)) <= MAX_SHORT_SHARE

    floors = [(size, gap) for size in SIZE_FLOORS for gap in (BASE_DISTANCE, TIGHT_DISTANCE) if size == SIZE_FLOORS[0] or gap == TIGHT_DISTANCE]
    found = next((pair for pair in floors if fits(pair[0], pair[1], compact=False)), None)
    if found is None:
        return None
    floor, start = found
    spatium = largest_fit(floor, 0.05, MAX_SPATIUM, lambda value: fits(value, start))
    distance = largest_fit(start, 1.0, MAX_DISTANCE, lambda value: fits(spatium, value, compact=False))
    score, base, probe = lay_out(src, work, spatium, distance, limit, variant)
    return score, base, spatium, distance, len(pymupdf.open(probe))


def legacy_command() -> str:
    """MuseScore 3's own executable; it is frozen at its last release."""
    return os.environ.get("MUSE_MS3") or str(pathlib.Path.home() / "Applications" / "MuseScore 3.app" / "Contents" / "MacOS" / "mscore")


def build_legacy(src: pathlib.Path, work: pathlib.Path, target: pathlib.Path, spatium: float, distance: float, info: Song) -> None:
    """The MuseScore 3 file, from the same MusicXML as the latest file, in its own program's format."""
    score = decode(src)
    style = styled(work, f"{info.name}-legacy.mss", info.title, spatium, distance)
    fonts = "".join(f"<{tag}>SF Pro Display</{tag}>" for tag in LEGACY_ONLY_FONTS)
    style.write_text(style.read_text(encoding="utf-8").replace('version="4.70"', 'version="3.02"').replace("</Style>", fonts + "</Style>"), encoding="utf-8")
    xml = work / f"{info.name}-{ORIGINAL}.musicxml"
    raw = work / f"{info.name}-legacy.mscz"
    done = subprocess.run([legacy_command(), "-S", str(style), "-o", str(raw), str(xml)], capture_output=True, text=True, timeout=240, stdin=subprocess.DEVNULL)
    if done.returncode:
        raise RuntimeError(f"MuseScore 3: {done.stderr[-300:]}")
    strip_title_frame(raw)
    legacy_signs(raw)
    big_signs(raw)
    widen_chord_bars(raw, score)
    target.write_bytes(raw.read_bytes())


def legacy_signs(mscz: pathlib.Path) -> None:
    """MuseScore 3's import already writes the built-in signs; only their size is ours."""
    with zipfile.ZipFile(mscz) as z:
        items = {info.filename: z.read(info.filename) for info in z.infolist()}
    for name, data in items.items():
        if name.endswith(".mscx"):
            text = data.decode("utf-8")
            for sym in ("segno", "coda"):
                text = text.replace(f"<text><sym>{sym}</sym></text>", f"<size>{LEGACY_SIGN_SIZE}</size><text><sym>{sym}</sym></text>")
            items[name] = text.encode("utf-8")
    with zipfile.ZipFile(mscz, "w", zipfile.ZIP_DEFLATED) as z:
        for key, data in items.items():
            z.writestr(key, data)


def port(src: pathlib.Path, out_dir: pathlib.Path, work: pathlib.Path) -> dict[str, object]:
    """Every file of 1 song's folder; returns what was built for the report."""
    info = song_of(src)
    work.mkdir(parents=True, exist_ok=True)
    (out_dir / info.name).mkdir(parents=True, exist_ok=True)
    report: dict[str, object] = {"song": info.name}
    original = fit(src, work, ORIGINAL, MAX_PAGES)
    if original is None:
        raise DoesNotFit(f"{info.name}: the original tone is over {MAX_PAGES} pages at the smallest size")
    _, base, spatium, distance, pages = original
    (out_dir / info.score_file).write_bytes(base.read_bytes())
    run("-o", str(out_dir / info.original_pdf), str(base))
    report["original"] = (spatium, distance, pages)
    # the legacy file reads the original tone's MusicXML, so it is built before anything overwrites it
    build_legacy(src, work, out_dir / info.legacy_file, spatium, distance, info)
    legacy_gap = scorefile.difference(out_dir / info.score_file, out_dir / info.legacy_file)
    report["legacy"] = legacy_gap[:6]
    if legacy_gap:
        (out_dir / info.legacy_file).unlink()
    if info.direction:
        tone = fit(src, work, TONE, MAX_PAGES)
        if tone is None:
            raise DoesNotFit(f"{info.name}: the favourite tone is over {MAX_PAGES} pages at the smallest size")
        _, base, spatium, distance, pages = tone
        export_view(transpose_options(info), base, out_dir / info.tone_pdf)
        report["tone"] = (spatium, distance, pages)
    sheet = fit(src, work, PIANO, 1) or fit(src, work, PIANO, MAX_PAGES)
    if sheet is None:
        raise DoesNotFit(f"{info.name}: the piano sheet is over {MAX_PAGES} pages at the smallest size")
    _, base, spatium, distance, pages = sheet
    export_view(transpose_options(info), base, out_dir / info.piano_pdf)
    keep = work / f"{info.name}-piano-final.mscz"
    keep.write_bytes(base.read_bytes())
    report["piano"] = (spatium, distance, pages)
    found = piano.parts(decode(src))
    probe_score = decode(src)
    report["parts"] = [(p.name, p.first, p.last, piano.staff_of([probe_score.bars[n - 1] for n in range(p.first, p.last + 1)])) for p in found]
    (work / f"{info.name}-report.json").write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    return report


def main() -> None:
    require_font()
    src_dir, out_dir, work = map(pathlib.Path, sys.argv[1:4])
    wanted = sys.argv[4:]
    sources = [src for src in song_sources(src_dir) if not wanted or src.stem.split("-")[0] in wanted]
    with concurrent.futures.ProcessPoolExecutor(max_workers=min(len(sources), MAX_WORKERS)) as pool:
        results = pool.map(port, sources, itertools.repeat(out_dir), itertools.repeat(work))
        reports = list(results)
    for report in reports:
        print(json.dumps(report, ensure_ascii=False), flush=True)
    previous = work / "build-report.json"
    earlier = json.loads(previous.read_text(encoding="utf-8")) if previous.exists() else {}
    earlier.update({str(r["song"]): r for r in reports})
    previous.write_text(json.dumps(earlier, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
