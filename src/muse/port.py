"""Port 1 source PDF: decode, write MusicXML, create the MuseScore file, transpose, export."""
from __future__ import annotations

import concurrent.futures
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import zipfile
from collections.abc import Callable
from typing import Any

import pymupdf

from muse import buildcache, musicxml, pdfcheck, piano, scorefile
from muse.model import Bar, Score
from muse.song import Song, decode, song_of
from muse.song import sources as song_sources

MAX_LAYOUT_ROUNDS = 40
MAX_MERGE_ROUNDS = 12
BARS_PER_LINE = 8
PIANO_LINE_LOAD = 16  # an empty bar costs 1 and a bar with notes 2, so notes keep 8 bars a line
NOTED_LOAD = 2
EMPTY_BASE = 0.25  # width of an empty bar beyond its chord names
CHORD_LETTER = 0.1  # width per letter of a chord name, gap included
MIN_EMPTY_STRETCH = 0.4
TIE_REACH = 2  # extra bars a line may take to avoid breaking on a tie
PICKUP_EVENTS = 2  # a first bar this thin shares the opening line as a pickup
SIZE_FLOORS = (1.2, 1.15)  # below 1.2 only for a song that fits its pages no other way; under 1.15 the fixed title outgrows chords
MAX_PAGES = 2
MAX_WORKERS = 3  # another project shares this machine
SIGN_LIFT = -2.5  # clears the chord beside a sign printed on its baseline
SIGN_SIZE = 25  # a sign may exceed the chords: about 2.5 times the default sign
MARKER_TYPES = {"segno": "segno", "coda": "codab"}
LEGACY_SIGN_SIZE = 25  # MuseScore 3 sizes the sign as text
LEGACY_ONLY_FONTS = ("staffFontFace", "systemFontFace", "beginFontFace", "continueFontFace", "endFontFace")  # its names for text styles
JUMP_LIFT = -7  # clears the chord row it would otherwise touch
JUMP_WORD_SIZE = 12  # below the chords
MAX_SHORT_SHARE = 0.15  # readers find many 2-bar lines sparse; smaller notation is preferred
CHORD_STRETCH = 0.45
STRETCH_CHOICES = (0.45, 0.15, 0.0)
MIDDLE_DISTANCE = 9.0
LEGACY_TOP_BORDER = 15  # MuseScore 3 lifts jump words and voltas into the title otherwise
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


LAUNCHES = {"count": 0}


def run(*args: str) -> None:
    LAUNCHES["count"] += 1
    done = subprocess.run(["mscore", *args], capture_output=True, text=True, timeout=240)
    if done.returncode:
        raise RuntimeError(f"mscore {args}: {done.stderr[-400:]}")


JUMPS = {
    "To Coda": "<Marker><style>repeat_right</style><text>To Coda</text><label>coda</label><markerType>coda</markerType></Marker>",
    "D.S. al Coda": "<Jump><text>D.S. al Coda</text><jumpTo>segno</jumpTo><playUntil>coda</playUntil><continueAt>codab</continueAt></Jump>",
}


def real_jumps(mscz: pathlib.Path, lift: bool = False) -> None:
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
        raised = f'<offset x="0" y="{SIGN_LIFT}"/>' if lift else ""
        for sym, label in (("segno", "segno"), ("coda", "codab")):
            # a visible marker is both the printed sign and the jump target; the style sizes it
            pattern = re.compile(r"\s*<StaffText>(?:(?!</StaffText>).)*?<text><sym>" + sym + r"</sym></text>\s*</StaffText>", re.S)
            for m in reversed(list(pattern.finditer(text))):
                start = text.rfind("<voice>", 0, m.start())
                element = f"<Marker><style>repeat_left</style><text><sym>{sym}</sym></text><label>{label}</label><markerType>{MARKER_TYPES[sym]}</markerType><markerCenterOnSymbol>0</markerCenterOnSymbol><musicSymbolSize>{SIGN_SIZE}</musicSymbolSize>{raised}</Marker>"
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


def piano_grid(score: Score, block: list[int]) -> list[int]:
    """Positions in a block that start a line, filling each line to its load; no break on a tie when the next bar avoids it."""
    starts = [0]
    load = 0
    for at, index in enumerate(block):
        cost = NOTED_LOAD if piano.has_notes(score.bars[index]) else 1
        if at and load + cost > PIANO_LINE_LOAD and not tied_out(score.bars[block[at - 1]]):
            starts.append(at)
            load = 0
        load += cost
    return starts


def opening_lines(score: Score, variant: str = TONE) -> list[int]:
    """0-based bars that start a line before any line has been judged too crowded."""
    if variant == PIANO:
        return [block[at] for block in sections(score) for at in piano_grid(score, block)]
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


def merge_candidates(lines: list[list[int]], fixed: set[int], refused: set[int], weight: Callable[[list[int]], int] = len, cap: int = BARS_PER_LINE) -> set[int]:
    """0-based line starts to try removing: shortest neighbours first, each line in at most 1 merge."""
    pairs = sorted(range(len(lines) - 1), key=lambda n: weight(lines[n]) + weight(lines[n + 1]))
    used: set[int] = set()
    joins = set()
    for n in pairs:
        start = lines[n + 1][0] - 1
        if n in used or n + 1 in used or start in fixed or start in refused:
            continue
        if weight(lines[n]) + weight(lines[n + 1]) <= cap:
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


def chord_room(bar: Bar) -> float:
    """Width of an empty piano bar: only what its chord names need."""
    return max(MIN_EMPTY_STRETCH, EMPTY_BASE + CHORD_LETTER * piano.chord_letters(bar))


def widen_chord_bars(mscz: pathlib.Path, score: Score, shrink_empty: bool = False) -> None:
    """Give bars with more chords more width, so a line rarely has to split for them; an empty piano bar takes only chord room."""
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
        if bar is None:
            return match.group(0)
        factor = chord_room(bar) if shrink_empty and not piano.has_notes(bar) else chord_stretch(len(bar.chords))
        return match.group(0) + f"<stretch>{factor:.2f}</stretch>" if factor != 1.0 else match.group(0)

    body = re.sub(r"<Measure[^>]*>", widen, body)
    items[name] = (text[:first] + body + text[end:]).encode("utf-8")
    with zipfile.ZipFile(mscz, "w", zipfile.ZIP_DEFLATED) as z:
        for key, data in items.items():
            z.writestr(key, data)


def transpose_options(song: Song) -> dict[str, Any] | None:
    if not song.direction:
        return None
    return {"mode": "to_key", "direction": song.direction, "targetKey": song.fifths, "transposeKeySignatures": True, "transposeChordNames": True, "useDoubleSharpsFlats": False, "transposeInterval": 0}


def export_view(options: dict[str, Any] | None, base: pathlib.Path, target: pathlib.Path) -> None:
    """Export a score, in the sheet's tone when it has its own: chord names, and so their widths, differ by key."""
    if options:
        run("-o", str(target), "--transpose", json.dumps(options), str(base))
    else:
        run("-o", str(target), str(base))


def lay_out(src: pathlib.Path, work: pathlib.Path, spatium: float, distance: float, pages: int | None, variant: str = TONE, lines: list[int] | None = None) -> tuple[Score, pathlib.Path, pathlib.Path]:
    """Build 1 sheet of a song at 1 size: no line wraps on its own and lines spread evenly over its pages; given lines are proved, never searched."""
    score = decode(src)
    info = song_of(src, score)
    if variant == PIANO:
        piano.reduce(score)
    lift_idle_cues(score)
    options = None if variant == ORIGINAL else transpose_options(info)
    song = f"{info.name}-{variant}"
    style = styled(work, f"{song}.mss", info.title, spatium, distance)
    xml = work / f"{song}.musicxml"
    base = work / f"{song}.mscz"
    probe = work / f"{song}-layout.pdf"
    starts = set(opening_lines(score, variant)) if lines is None else set(lines)
    load = (lambda line: sum(NOTED_LOAD if piano.has_notes(score.bars[n - 1]) else 1 for n in line)) if variant == PIANO else len
    cap = PIANO_LINE_LOAD if variant == PIANO else BARS_PER_LINE
    tied = [tied_out(bar) for bar in score.bars]
    fixed = {block[0] for block in sections(score)}

    lifted = {"signs": False}

    def build() -> None:
        xml.write_text(musicxml.write(score), encoding="utf-8")
        run("-S", str(style), "-o", str(base), str(xml))
        strip_title_frame(base)
        real_jumps(base, lifted["signs"])
        big_signs(base)
        widen_chord_bars(base, score, variant == PIANO)
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

    if lines is None:
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
            joins = merge_candidates(rows(score), fixed, refused, load, cap)
            if not joins:
                break
            starts -= joins
            declared, bad = measure()
            undone = {at for row in declared if any(n in row for n in bad) for at in joins if row[0] - 1 < at <= row[-1] - 1}
            refused |= undone
            starts |= undone
        measure()
    elif measure()[1]:
        raise DoesNotFit(f"{song}: saved lines no longer fit")
    found = len(pymupdf.open(probe))
    if pages is not None and found > pages:
        raise DoesNotFit(f"{song}: {found} pages, over {pages}")
    if balance(score, probe):
        build()
        if len(pymupdf.open(probe)) != found:
            raise DoesNotFit(f"{song}: even pages need {len(pymupdf.open(probe))} pages, not {found}")
    if pdfcheck.sign_on_chord(probe):
        lifted["signs"] = True
        build()
    found_problems = pdfcheck.all_problems(probe, info.title)
    if found_problems:
        raise DoesNotFit(f"{song}: {found_problems[:3]}")
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


def choose_stretch(src: pathlib.Path, work: pathlib.Path, spatium: float, distance: float, limit: int, variant: str) -> None:
    """Chord width helps some songs and starves others, so each sheet keeps the share leaving fewest short lines."""
    global CHORD_STRETCH  # noqa: PLW0603
    best: tuple[float, float] | None = None
    for share in STRETCH_CHOICES:
        CHORD_STRETCH = share
        try:
            laid, _, _ = lay_out(src, work, spatium, distance, limit, variant)
        except DoesNotFit:
            continue
        found = short_line_share(rows(laid))
        if best is None or found < best[0]:
            best = (found, share)
    CHORD_STRETCH = best[1] if best else STRETCH_CHOICES[0]


def line_starts(score: Score) -> list[int]:
    return [row[0] - 1 for row in rows(score)]


def prove(src: pathlib.Path, work: pathlib.Path, variant: str, saved: dict[str, Any]) -> tuple[Score, pathlib.Path, float, float, int] | None:
    """Rebuild a saved decision once; None sends the sheet back to the search."""
    global CHORD_STRETCH  # noqa: PLW0603
    CHORD_STRETCH = float(saved["stretch"])
    try:
        laid, base, probe = lay_out(src, work, float(saved["spatium"]), float(saved["distance"]), int(saved["pages"]), variant, list(saved["lines"]))
    except DoesNotFit:
        return None
    if not acceptable(laid, variant):
        return None
    return laid, base, float(saved["spatium"]), float(saved["distance"]), len(pymupdf.open(probe))


def acceptable(laid: Score, variant: str) -> bool:
    if variant == PIANO:
        noted = {bar.number for bar in laid.bars if piano.has_notes(bar)}
        letters = {bar.number: piano.chord_letters(bar) for bar in laid.bars}
        return not piano.sparse_lines([row[0] for row in rows(laid)], noted, {block[0] + 1 for block in sections(laid)}, len(laid.bars), letters)
    return short_line_share(rows(laid)) <= MAX_SHORT_SHARE


def fit(src: pathlib.Path, work: pathlib.Path, variant: str, limit: int) -> tuple[Score, pathlib.Path, float, float, int] | None:
    """The largest notation of 1 sheet within a page limit, its lines then spread over the height left."""
    tried: dict[tuple[float, float, float, bool], bool] = {}

    def fits(spatium: float, distance: float, compact: bool = True) -> bool:
        memo = (spatium, distance, CHORD_STRETCH, compact)
        if memo not in tried:
            tried[memo] = attempt(spatium, distance, compact)
        return tried[memo]

    def attempt(spatium: float, distance: float, compact: bool) -> bool:
        try:
            laid, _, _ = lay_out(src, work, spatium, distance, limit, variant)
        except DoesNotFit:
            return False
        return not compact or acceptable(laid, variant)

    floors = [(size, gap) for size in SIZE_FLOORS for gap in (BASE_DISTANCE, MIDDLE_DISTANCE, TIGHT_DISTANCE) if size == SIZE_FLOORS[0] or gap != BASE_DISTANCE]
    found = next((pair for pair in floors if fits(pair[0], pair[1], compact=False)), None)
    if found is None:
        return None
    floor, start = found
    choose_stretch(src, work, floor, start, limit, variant)
    spatium = largest_fit(floor, 0.05, MAX_SPATIUM, lambda value: fits(value, start))
    settled, _, _ = lay_out(src, work, spatium, start, limit, variant)
    lines = line_starts(settled)  # the line gap changes height alone, so its search keeps these lines

    def gap_fits(value: float) -> bool:
        try:
            lay_out(src, work, spatium, value, limit, variant, lines)
        except DoesNotFit:
            return False
        return True

    distance = largest_fit(start, 1.0, MAX_DISTANCE, gap_fits)
    score, base, probe = lay_out(src, work, spatium, distance, limit, variant, lines)
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
    style.write_text(re.sub(r"<staffUpperBorder>[^<]*</staffUpperBorder>", f"<staffUpperBorder>{LEGACY_TOP_BORDER}</staffUpperBorder>", style.read_text(encoding="utf-8")), encoding="utf-8")
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


def lift_idle_cues(score: Score) -> None:
    """A cue on a staff with no notes in its bar moves up, or that staff could never hide."""
    for bar in score.bars:
        if not any(e.heads for v in bar.voices2 for e in v):
            bar.texts = [*bar.texts, *bar.texts2]
            bar.texts2 = []


def legacy_signs(mscz: pathlib.Path) -> None:
    """MuseScore 3's import already writes the built-in signs; only their size is ours."""
    with zipfile.ZipFile(mscz) as z:
        items = {info.filename: z.read(info.filename) for info in z.infolist()}
    for name, data in items.items():
        if name.endswith(".mscx"):
            text = data.decode("utf-8")
            text = text.replace("<hideEmptyStaves>1</hideEmptyStaves>", "<hideEmptyStaves>1</hideEmptyStaves>\n    <dontHidStavesInFirstSystm>0</dontHidStavesInFirstSystm>", 1)  # MuseScore 3 spells it this way
            for sym in ("segno", "coda"):
                text = text.replace(f"<text><sym>{sym}</sym></text>", f"<size>{LEGACY_SIGN_SIZE}</size><text><sym>{sym}</sym></text>")
            items[name] = text.encode("utf-8")
    with zipfile.ZipFile(mscz, "w", zipfile.ZIP_DEFLATED) as z:
        for key, data in items.items():
            z.writestr(key, data)


def sheet(src: pathlib.Path, work: pathlib.Path, variant: str, limits: tuple[int, ...], saved: dict[str, Any] | None) -> tuple[Score, pathlib.Path, float, float, int]:
    """A saved decision re-proved in 1 pass, or the search."""
    found = prove(src, work, variant, saved) if saved else None
    for limit in limits:
        found = found or fit(src, work, variant, limit)
    if found is None:
        raise DoesNotFit(f"{src.stem}: the {variant} sheet is over {limits[-1]} pages at the smallest size")
    return found


def decision(found: tuple[Score, pathlib.Path, float, float, int]) -> dict[str, Any]:
    return {"spatium": found[2], "distance": found[3], "pages": found[4], "stretch": CHORD_STRETCH, "lines": line_starts(found[0])}


def port(src: pathlib.Path, out_dir: pathlib.Path, work: pathlib.Path) -> dict[str, object]:
    """Every out-of-date file of 1 song's folder; returns what was built for the report."""
    began = time.monotonic()
    LAUNCHES["count"] = 0
    info = song_of(src)
    work.mkdir(parents=True, exist_ok=True)
    (out_dir / info.name).mkdir(parents=True, exist_ok=True)
    report: dict[str, object] = {"song": info.name}
    state = buildcache.load(info.name)
    shared = buildcache.code_files(skip=buildcache.PIANO_ONLY_CODE | buildcache.CHECK_ONLY_CODE)
    every = buildcache.code_files(skip=buildcache.CHECK_ONLY_CODE)
    built = []

    def current(variant: str, code: list[pathlib.Path], outputs: list[pathlib.Path]) -> tuple[str, bool]:
        key = buildcache.key_of(src, repr(info), code)
        return key, buildcache.fresh(state.get(variant), key, outputs)

    def keep(variant: str, key: str, outputs: list[pathlib.Path], found: tuple[Score, pathlib.Path, float, float, int]) -> None:
        state[variant] = buildcache.record(key, outputs, decision=decision(found))
        buildcache.save(info.name, state)
        report[variant] = (found[2], found[3], found[4])
        built.append(variant)

    def old(variant: str) -> dict[str, Any] | None:
        return state.get(variant, {}).get("decision")

    outputs = [out_dir / info.score_file, out_dir / info.original_pdf, out_dir / info.legacy_file]
    key, ready = current(ORIGINAL, shared, outputs)
    if not ready:
        found = sheet(src, work, ORIGINAL, (MAX_PAGES,), old(ORIGINAL))
        (out_dir / info.score_file).write_bytes(found[1].read_bytes())
        shutil.copyfile(work / f"{info.name}-{ORIGINAL}-layout.pdf", out_dir / info.original_pdf)
        # the legacy file reads the original tone's MusicXML, so it is built before anything overwrites it
        build_legacy(src, work, out_dir / info.legacy_file, found[2], found[3], info)
        legacy_gap = scorefile.difference(out_dir / info.score_file, out_dir / info.legacy_file)
        report["legacy"] = legacy_gap[:6]
        if legacy_gap:
            (out_dir / info.legacy_file).unlink()
            outputs = outputs[:2]
        keep(ORIGINAL, key, outputs, found)
    if info.direction:
        outputs = [out_dir / info.tone_pdf]
        key, ready = current(TONE, shared, outputs)
        if not ready:
            found = sheet(src, work, TONE, (MAX_PAGES,), old(TONE))
            shutil.copyfile(work / f"{info.name}-{TONE}-layout.pdf", out_dir / info.tone_pdf)
            keep(TONE, key, outputs, found)
    final = work / f"{info.name}-piano-final.mscz"
    outputs = [out_dir / info.piano_pdf, final]
    key, ready = current(PIANO, every, outputs)
    if not ready:
        found = sheet(src, work, PIANO, (1, MAX_PAGES), old(PIANO))
        shutil.copyfile(work / f"{info.name}-{PIANO}-layout.pdf", out_dir / info.piano_pdf)
        final.write_bytes(found[1].read_bytes())
        keep(PIANO, key, outputs, found)
    found_parts = piano.parts(decode(src))
    probe_score = decode(src)
    report["parts"] = [(p.name, p.first, p.last, piano.staff_of([probe_score.bars[n - 1] for n in range(p.first, p.last + 1)])) for p in found_parts]
    report["built"] = built
    report["launches"] = LAUNCHES["count"]
    report["seconds"] = round(time.monotonic() - began, 1)
    (work / f"{info.name}-report.json").write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    return report


def free_workers(count: int, cores: int | None = None, load: float | None = None) -> int:
    """Workers the machine can run now: its free cores, at least 1."""
    cores = cores if cores is not None else os.cpu_count() or 1
    load = load if load is not None else os.getloadavg()[0]
    return max(1, min(count, MAX_WORKERS, int(cores - load)))


def main() -> None:
    require_font()
    src_dir, out_dir, work = map(pathlib.Path, sys.argv[1:4])
    wanted = sys.argv[4:]
    sources = [src for src in song_sources(src_dir) if not wanted or src.stem.split("-")[0] in wanted]
    began = time.monotonic()
    with concurrent.futures.ProcessPoolExecutor(max_workers=free_workers(len(sources))) as pool:
        jobs = {pool.submit(port, src, out_dir, work): src for src in sources}
        reports = []
        refused = []
        for job in concurrent.futures.as_completed(jobs):
            try:
                reports.append(job.result())
                print(json.dumps(reports[-1], ensure_ascii=False), flush=True)
            except (DoesNotFit, RuntimeError, OSError, subprocess.SubprocessError) as error:  # 1 song's failure never stops the others
                refused.append(jobs[job].stem)
                print(f"refused {jobs[job].stem}: {error}", flush=True)
    previous = work / "build-report.json"
    earlier = json.loads(previous.read_text(encoding="utf-8")) if previous.exists() else {}
    earlier.update({str(r["song"]): r for r in reports})
    previous.write_text(json.dumps(earlier, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"songs {len(reports)} launches {sum(int(str(r['launches'])) for r in reports)} seconds {time.monotonic() - began:.1f}")
    if refused:
        raise SystemExit(f"refused: {refused}")


if __name__ == "__main__":
    main()
