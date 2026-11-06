"""Port 1 source PDF: decode, write MusicXML, create the MuseScore file, transpose, export."""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from collections.abc import Callable
from typing import Any

import pymupdf

from muse import buildcache, chordnames, chordseat, musicxml, pdfcheck, pdftext, piano, scorefile
from muse.model import Bar, Chord, Score
from muse.render import BATCHER
from muse.song import LEGACY_FILES, Song, decode, song_of
from muse.song import sources as song_sources

MAX_LAYOUT_ROUNDS = 40
MAX_MERGE_ROUNDS = 12
LETTER_WIDTH = 0.52  # of the chord size
CHORD_GAP = 0.75  # of the chord size, between 2 chords
CHORD_SIZE_PER_SPACE = 8.0  # printed chord points per unit of the style's spatium
LINE_WIDTH = 540.0  # points of a line after its clef
TRY_CEILING = 24  # bars tried on a line, only to limit tries; what fits decides
PHRASE = 4  # bars from a section's start that make a phrase boundary
PHRASE_PULL = 1.5  # a phrase boundary is worth this many bars left off a line
BACK_REACH = 2  # bars a line may give back for a better break
SPLIT_SHARE = 4  # a crowded line gives up this share of its bars
NOTE_ROOM = 9.0  # points per note event, per unit of spatium
EMPTY_BAR_ROOM = 12.0  # points an empty bar needs, per unit of spatium
SIZE_FLOORS = (1.2, 1.15)  # below 1.2 only for a song that fits its pages no other way; under 1.15 the fixed title outgrows chords
SIZE_CAP = 2.0  # the largest notation any sheet takes
PAGE_FILL = 0.85  # how far down every page the music must reach
MAX_WORKERS = 10
SEAT_PASSES = 5
LAYOUT_FILES = ("port.py", "chordseat.py", "piano.py", "chordnames.py", "musicxml.py", "style.mss")
FINAL_SPLITS = 8
FINAL_STEPS = 6  # sizes to step down when the chosen layout fails its final checks
SHEET_LAUNCH_CAP = 400
FINAL_LAUNCH_CAP = 600
PIANO_LAUNCH_CAP = 500
NEAR_REACH = 0.5  # how far above a saved size a seeded search looks
SIZE_STEP = 0.05
WAVE = 8  # sizes laid out side by side in the first wave of a search
BORROW_FROM = 4  # bars a line must hold to lend its last one to a short line below
CLIMB_START, CLIMB_STEP, CLIMB_STEPS = 1.3, 0.1, 8
SIGN_CLEARANCE = 3.0  # spaces a chord moves right off a sign printed on its baseline
TARGET_HOLD = '<autoplace>0</autoplace><offset x="1" y="-3"/>'
SIGN_STEP = -6  # spaces a jump sign steps left of the sign beside it
SIGN_RISE = -2.5  # spaces above the staff, in its own chord row
SIGN_BAR_STRETCH = 2.5  # a bar holding 2 signs widens to hold both
SIGN_SIZE = 25  # a sign may exceed the chords: about 2.5 times the default sign
MARKER_TYPES = {"segno": "segno", "coda": "codab"}
LEGACY_SIGN_SIZE = 25  # MuseScore 3 sizes the sign as text
LEGACY_ONLY_FONTS = ("staffFontFace", "systemFontFace", "beginFontFace", "continueFontFace", "endFontFace")  # its names for text styles
MAX_SHORT_SHARE = 0.15  # readers find many 2-bar lines sparse; smaller notation is preferred
RELAXED_SHORT_SHARE = 0.30  # when no size holds a song to the base share
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
SEARCHED: dict[str, Any] = {"rules": None}  # the layout rules the sheet being built was last searched under
SEARCH_CAP = {"at": 0, "hit": 0}  # launch count where a sheet's search stops, 0 leaving it open; and how often a cap stopped it


def run(*args: str) -> None:
    if SEARCH_CAP["at"] and LAUNCHES["count"] >= SEARCH_CAP["at"]:
        SEARCH_CAP["hit"] += 1
        raise DoesNotFit("the search reached its launch cap")
    LAUNCHES["count"] += 1
    done = subprocess.run(["mscore", *args], capture_output=True, text=True, timeout=240)
    if done.returncode:
        raise RuntimeError(f"mscore {args}: {done.stderr[-400:]}")


JUMPS = {
    "To Coda": "<Marker><style>repeat_right</style><text><sym>coda</sym></text><label>coda</label><markerType>coda</markerType><markerCenterOnSymbol>0</markerCenterOnSymbol><musicSymbolSize>25</musicSymbolSize></Marker>",
    "D.S. al Coda": "<Jump><musicSymbolSize>25</musicSymbolSize><text><sym>segno</sym></text><jumpTo>segno</jumpTo><playUntil>coda</playUntil><continueAt>codab</continueAt></Jump>",
}
BOX = '<frameType>1</frameType><frameWidth>0.45</frameWidth><framePadding>0.4</framePadding><frameFgColor r="{}" g="{}" b="{}" a="255"/>'
SIGN_BOX = {"segno": BOX.format(220, 0, 0), "coda": BOX.format(0, 0, 220)}


def real_jumps(mscz: pathlib.Path, clear_chord: bool = False) -> None:
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
                chord = text.find("<Harmony>", start)
                if clear_chord and chord != -1 and chord < text.find("</Measure>", start):
                    # a marker ignores its own offset in layout, so the chord beside it makes the room
                    at = chord + len("<Harmony>")
                    text = text[:at] + f'<offset x="{SIGN_CLEARANCE}" y="0"/>' + text[at:]
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
    for page in pdftext.open(pdf):
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


def last_before(where: list[tuple[int, float, int]], page_no: int) -> int:
    """The line a page with no printed bar number continues: the last one numbered on an earlier page."""
    return max(item for item in where if item[0] < page_no)[2]


def crowded_rows(pdf: pathlib.Path, firsts: list[int]) -> set[int]:
    """First bar of every line holding chords that run together, matched by page and height."""
    where = []
    for page_no, page in enumerate(pdftext.open(pdf)):
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    if span["text"].strip().isdigit() and span["bbox"][0] < 60 and "Italic" in span["font"] and int(span["text"]) in firsts:
                        where.append((page_no, span["bbox"][1], int(span["text"])))
    where.append((0, 0.0, 1))
    crowded = set()
    for page_no, y, _ in pdfcheck.chord_collisions(pdf):
        same_page = [item for item in where if item[0] == page_no]
        crowded.add(min(same_page, key=lambda item: abs(item[1] - y))[2] if same_page else last_before(where, page_no))
    for page_no, y, _ in [*pdfcheck.past_margin(pdf), *pdfcheck.text_collisions(pdf)]:
        above = [item for item in where if item[0] == page_no and item[1] <= y]
        same_page = above or [item for item in where if item[0] == page_no]
        crowded.add(max(same_page, key=lambda item: item[1])[2] if same_page else last_before(where, page_no))
    return crowded


def must_start_line(bar: Bar, previous: Bar | None) -> bool:
    """A section mark or a lyric label starts a new block of lines."""
    return previous is None or bool(bar.rehearsal or bar.segno or bar.coda or previous.final or piano.label_of(bar))


def sections(score: Score) -> list[list[int]]:
    """Bar indexes of each block; a tie into a section mark keeps the bars together."""
    blocks: list[list[int]] = []
    for index, bar in enumerate(score.bars):
        previous = score.bars[index - 1] if index else None
        if must_start_line(bar, previous) and (previous is None or not tied_out(previous)):
            blocks.append([])
        blocks[-1].append(index)
    return blocks


def chord_name_length(chord: Chord) -> int:
    return len(chord.root) + bool(chord.root_alter) + len(chordnames.stored(chord.kind)) + (len(chord.bass) + 1 + bool(chord.bass_alter) if chord.bass else 0)


def bar_room(bar: Bar, spatium: float) -> float:
    """Points a bar asks for: chords sit on evenly shared beats, so the longest chord and its gap times the chords sharing the bar; or its notes, whichever is more."""
    size = CHORD_SIZE_PER_SPACE * spatium
    longest = max((chord_name_length(chord) for chord in bar.chords), default=0)
    chords = (longest * LETTER_WIDTH + CHORD_GAP) * size * len(bar.chords)
    notes = sum(len(voice) for voice in bar.voices) * NOTE_ROOM * spatium if piano.has_notes(bar) else 0.0
    return max(chords, notes, EMPTY_BAR_ROOM * spatium)


def line_room(bars: list[Bar], spatium: float) -> float:
    """Points a line asks for; every empty bar takes the width of the widest, since chord text never widens 1 bar."""
    empty = [bar_room(bar, spatium) for bar in bars if not piano.has_notes(bar)]
    return sum(bar_room(bar, spatium) for bar in bars if piano.has_notes(bar)) + max(empty, default=0.0) * len(empty)


def break_cost(bars: list[Bar], stop: int, full: int) -> tuple[bool, float]:
    """A break is awkward before a chordless bar, on a tie or stranding 1 bar; otherwise fuller is better, and a phrase boundary is worth a little."""
    awkward = stop < len(bars) and (tied_out(bars[stop - 1]) or not bars[stop].chords or len(bars) - stop == 1)
    return (awkward, full - stop - (PHRASE_PULL if stop % PHRASE == 0 else 0.0))


def fill_block(bars: list[Bar], spatium: float) -> list[int]:
    """Positions starting a line in a section: each line fills until the next bar will not fit, then breaks at the best nearby place."""
    starts = []
    at = 0
    while at < len(bars):
        starts.append(at)
        end = at + 1
        while end < len(bars) and end - at < TRY_CEILING and line_room(bars[at:end + 1], spatium) <= LINE_WIDTH:
            end += 1

        options = [(break_cost(bars, stop, end), stop) for stop in range(max(at + 1, end - BACK_REACH), end + 1)]
        at = min(options)[1]
    return starts


def opening_lines(score: Score, variant: str = TONE, spatium: float = 1.2) -> list[int]:
    """0-based bars that start a line, from estimated widths, before any render has judged them."""
    return [block[at] for block in sections(score) for at in fill_block([score.bars[i] for i in block], spatium)]


def short_line_share(lines: list[list[int]]) -> float:
    """Share of lines with 2 bars or fewer."""
    return sum(1 for line in lines if len(line) <= 2) / len(lines)


def ease_line(starts: set[int], row: list[int], fixed: set[int], tied: list[bool]) -> None:
    """A crowded line hands its last bar to the next line of its section; only the last line of a section splits."""
    following = row[-1]  # the 0-based start of the next line equals the 1-based number of this line's last bar
    if following in starts and following not in fixed and len(row) > 1:
        starts.discard(following)
        starts.add(following - 1)
    else:
        starts.add(split_at(row[0] - 1, row[-1], tied))


def merge_candidates(lines: list[list[int]], fixed: set[int], refused: set[int], fits: Callable[[list[int]], bool]) -> set[int]:
    """0-based line starts to try removing: shortest neighbours first, each line in at most 1 merge."""
    pairs = sorted(range(len(lines) - 1), key=lambda n: len(lines[n]) + len(lines[n + 1]))
    used: set[int] = set()
    joins = set()
    for n in pairs:
        start = lines[n + 1][0] - 1
        if n in used or n + 1 in used or start in fixed or start in refused:
            continue
        if fits(lines[n] + lines[n + 1]):
            joins.add(start)
            used |= {n, n + 1}
    return joins


def split_at(first: int, end: int, tied: list[bool]) -> int:
    """Where a line that will not fit gives up its last bars: no stranded single bar, then off a tie, then taking as little as helps."""
    middle = end - max(1, (end - first) // SPLIT_SHARE)

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
    doc = pdftext.open(pdf)
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


class SheetRefused(RuntimeError):
    """A final-PDF check no size can change; the song stops at once instead of searching."""


class DoesNotFit(Exception):
    """This size does not fit the song's page count."""


def styled(work: pathlib.Path, name: str, title: str, spatium: float, distance: float) -> pathlib.Path:
    """A copy of the style at 1 notation size and line gap, with the song title at the top right of every page."""
    text = STYLE.read_text(encoding="utf-8")
    for tag, value in (("Spatium", spatium), ("minSystemDistance", distance), ("maxSystemDistance", distance), ("oddHeaderR", title), ("evenHeaderR", title)):
        text, count = re.subn(rf"<{tag}>[^<]*</{tag}>", f"<{tag}>{value}</{tag}>", text)
        if count != 1:
            raise RuntimeError(f"style has no single {tag}")
    text = text.replace("</Style>", f"<chordStyle>custom</chordStyle><chordDescriptionFile>{chordnames.chord_file(work)}</chordDescriptionFile></Style>")
    path = work / name
    path.write_text(text, encoding="utf-8")
    return path


def boxed_sign(element: str) -> str:
    """1 Marker or Jump as its sign alone in its colored box; the jump it performs is untouched."""
    kind = "segno" if element.startswith("<Jump") or "<label>segno" in element else "coda"
    bare = re.sub(r"<offset[^>]*/>", "", element)
    bare = re.sub(r"<text>.*?</text>", f"<text><sym>{kind}</sym></text>", bare, flags=re.S)
    if "</style>" in bare:
        held = TARGET_HOLD if "repeat_left" in bare or "Repeat Text Left" in bare else ""  # a target held down in its own line's chord row
        return bare.replace("</style>", "</style>" + held + SIGN_BOX[kind], 1)
    return bare.replace(">", ">" + SIGN_BOX[kind], 1)


def beside_each_other(text: str) -> str:
    """In a bar holding 2 jump signs the Jump steps left, so both stay in the chord row of their own bar."""
    measures = re.split(r"(?=<Measure)", text)
    for at, measure in enumerate(measures):
        if "<Jump>" in measure and "<label>coda</label>" in measure:
            measure = re.sub(r"^<Measure([^>]*)>", lambda m: f"<Measure{m[1]}><stretch>{SIGN_BAR_STRETCH}</stretch>", measure, count=1)
            measures[at] = measure.replace("<Jump>", f'<Jump><autoplace>0</autoplace><offset x="{SIGN_STEP}" y="{SIGN_RISE}"/>', 1)
    return "".join(measures)


def big_signs(mscz: pathlib.Path) -> None:
    """Every jump sign in its box, with no words."""
    with zipfile.ZipFile(mscz) as z:
        items = {info.filename: z.read(info.filename) for info in z.infolist()}
    for name, data in items.items():
        if name.endswith(".mscx"):
            items[name] = beside_each_other(re.sub(r"<(Marker|Jump)>.*?</\1>", lambda m: boxed_sign(m[0]), data.decode("utf-8"), flags=re.S)).encode("utf-8")
    with zipfile.ZipFile(mscz, "w", zipfile.ZIP_DEFLATED) as z:
        for key, data in items.items():
            z.writestr(key, data)


def transpose_options(song: Song) -> dict[str, Any] | None:
    if not song.direction:
        return None
    return {"mode": "to_key", "direction": song.direction, "targetKey": song.fifths, "transposeKeySignatures": True, "transposeChordNames": True, "useDoubleSharpsFlats": False, "transposeInterval": 0}


def export_view(options: dict[str, Any] | None, base: pathlib.Path, target: pathlib.Path, positions: bool = True) -> None:
    """Export a score, in the sheet's tone when it has its own: chord names, and so their widths, differ by key."""
    BATCHER.render(base, [target, target.with_suffix(".mpos")] if positions else [target], options)  # the positions are the program's own record of this same layout


def borrowings(lines: list[list[int]], fixed: set[int]) -> list[tuple[int]]:
    """0-based starts of short lines that can take the last bar of a line above with bars to spare."""
    found = []
    for above, line in zip(lines, lines[1:], strict=False):
        if len(line) <= 2 and len(above) >= BORROW_FROM and line[0] - 1 not in fixed:
            found.append((line[0] - 1,))
    return found


def clear_lone_bars(starts: set[int], fixed: set[int], total: int, fits: Callable[[set[int]], bool]) -> set[int]:
    """Give every lone bar a neighbour, keeping the first arrangement that fits and strands nothing new."""
    for _ in range(total):
        lone = [number - 1 for number in piano.lone_lines(sorted(s + 1 for s in starts), total)]
        changed = False
        for bar in lone:
            for move in piano.lone_moves(starts, bar, total, fixed):
                if len(piano.lone_lines(sorted(s + 1 for s in move), total)) < len(lone) and fits(move):
                    starts = move
                    changed = True
                    break
            if changed:
                break
        if not changed:
            break
    return starts


def seat_chords(score_path: pathlib.Path, pdf: pathlib.Path, render: Callable[[], None], song: str, noted: set[int] | None = None, reach: float | None = None) -> None:
    """Even out empty bars where asked and move beat-1 chords to their barlines by measuring the render, until nothing is left to move."""
    try:
        for _ in range(SEAT_PASSES if noted is not None else 0):  # bars first: evening them moves every barline
            if not chordseat.equalize(score_path, pdf, noted or set()):
                break
            render()
        for _ in range(SEAT_PASSES):
            if not chordseat.align(score_path, pdf, reach):
                break
            render()
    except ValueError:
        return  # chords run together, so the text layer merges them; the crowding check splits that line


def settle_chord_starts(starts: set[int], chorded: set[int], marks: set[int], total: int) -> set[int]:
    """No line starts on a bar without its own chord: its chord's bar goes down with it, or it stays up on its chord's line; a section mark stays where it is."""
    settled = set(starts)
    for bar in sorted(starts):
        if bar == 0 or bar in chorded or bar in marks or bar not in settled:
            continue
        settled.discard(bar)
        earlier = max((s for s in settled if s < bar), default=0)
        back = max((c for c in chorded if earlier < c < bar), default=None)
        if back is not None:
            settled.add(back)
            continue
        later = min((s for s in settled if s > bar), default=total)
        ahead = min((c for c in chorded if bar < c < later), default=None)
        if ahead is not None:
            settled.add(ahead)
    return settled


def refuse_fixed(probe: pathlib.Path, title: str, song: str) -> None:
    """Stop the song on a check no size can change."""
    fixed_problems = pdfcheck.margin_words(probe) + pdfcheck.foreign_fonts(probe) + pdfcheck.title_problems(probe, title) + pdfcheck.sign_problems(probe)
    if fixed_problems:
        raise SheetRefused(f"{song}: {fixed_problems[:3]}")


IMPORTS: dict[str, threading.Lock] = {}
IMPORTS_LOCK = threading.Lock()


def imported(score: Score, work: pathlib.Path, sheet: str, style: pathlib.Path, clear: bool) -> pathlib.Path:
    """1 import of a sheet with no line breaks, its signs set; every trial edits a copy of it."""
    flags = [(bar.new_system, bar.new_page) for bar in score.bars]
    for bar in score.bars:
        bar.new_system = bar.new_page = False
    text = musicxml.write(score)
    for bar, (line, page) in zip(score.bars, flags, strict=True):
        bar.new_system, bar.new_page = line, page
    whole = hashlib.sha256()
    for part in (text, style.read_text(encoding="utf-8"), str(clear), *(file.read_text(encoding="utf-8") for file in buildcache.code_files())):
        whole.update(part.encode("utf-8"))
    path = work / f"{sheet}-import-{whole.hexdigest()[:12]}.mscz"  # keyed on what it was built from, so a stale import is never reused
    with IMPORTS_LOCK:
        lock = IMPORTS.setdefault(str(path), threading.Lock())
    with lock:
        if not path.exists():
            xml = path.with_suffix(".musicxml")
            xml.write_text(text, encoding="utf-8")
            fresh = path.with_name(path.stem + "-fresh.mscz")
            run("-S", str(style), "-o", str(fresh), str(xml))
            strip_title_frame(fresh)
            real_jumps(fresh, clear)
            big_signs(fresh)
            fresh.replace(path)
    return path


def edited(source: pathlib.Path, target: pathlib.Path, spatium: float, distance: float, score: Score) -> None:
    """A copy of an import at 1 size and line gap, breaking lines and pages where the score says."""
    with zipfile.ZipFile(source) as old:
        items = {name: old.read(name) for name in old.namelist()}
    for name, data in items.items():
        if not name.endswith((".mss", ".mscx")):
            continue
        text = data.decode("utf-8")
        text = re.sub(r"<([Ss])patium>[^<]*</[Ss]patium>", lambda m: f"<{m[1]}patium>{spatium}</{m[1]}patium>", text)
        for tag in ("minSystemDistance", "maxSystemDistance"):
            text = re.sub(rf"<{tag}>[^<]*</{tag}>", f"<{tag}>{distance}</{tag}>", text)
        if name.endswith(".mscx"):
            text = re.sub(r"<LayoutBreak>.*?</LayoutBreak>\s*", "", text, flags=re.S)
            pieces = re.split(r"(?=<Measure)", text)
            count = 0
            for at, piece in enumerate(pieces):
                if piece.startswith("<Measure"):
                    count += 1
                    if count < len(score.bars) and score.bars[count].new_system:
                        kind = "page" if score.bars[count].new_page else "line"
                        pieces[at] = re.sub(r"(</Measure>)", rf"<LayoutBreak><subtype>{kind}</subtype></LayoutBreak>\1", piece, count=1)
            text = "".join(pieces)
        items[name] = text.encode("utf-8")
    fresh = target.with_name(target.stem + "-edit.mscz")
    with zipfile.ZipFile(fresh, "w", zipfile.ZIP_DEFLATED) as new:
        for name, data in items.items():
            new.writestr(name, data)
    fresh.replace(target)


def lay_out(src: pathlib.Path, work: pathlib.Path, spatium: float, distance: float, pages: int | None, variant: str = TONE, lines: list[int] | None = None, seated: bool = True) -> tuple[Score, pathlib.Path, pathlib.Path]:
    """Build 1 sheet of a song at 1 size: no line wraps on its own and lines spread evenly over its pages; given lines are proved, never searched."""
    score = decode(src)
    info = song_of(src, score)
    if variant == PIANO:
        piano.reduce(score)
    lift_idle_cues(score)
    options = None if variant == ORIGINAL else transpose_options(info)
    song = f"{info.name}-{variant}-{spatium:g}-{distance:g}{'' if seated else '-trial'}"  # every layout owns its files, so layouts run side by side
    style = styled(work, f"{song}.mss", info.title, spatium, distance)
    base = work / f"{song}.mscz"
    probe = work / f"{song}-layout.pdf"
    starts = set(opening_lines(score, variant, spatium)) if lines is None else set(lines)
    def joins_fit(line: list[int]) -> bool:
        return len(line) <= TRY_CEILING and line_room([score.bars[n - 1] for n in line], spatium) <= LINE_WIDTH

    tied = [tied_out(bar) for bar in score.bars]
    fixed = {block[0] for block in sections(score)}

    clearing = {"signs": False}

    def build() -> None:
        edited(imported(score, work, f"{info.name}-{variant}", style, clearing["signs"]), base, spatium, distance, score)  # every layout edits 1 import
        export_view(options, base, probe)
        if seated:  # a search trial stops here: only the chosen layout pays to be measured
            noted = {bar.number for bar in score.bars if piano.has_notes(bar)}
            seat_chords(base, probe, lambda: export_view(options, base, probe), song, noted)

    chorded = {index for index, bar in enumerate(score.bars) if bar.chords}

    def measure() -> tuple[list[list[int]], set[int]]:
        nonlocal starts
        starts = settle_chord_starts(starts, chorded, fixed, len(score.bars))
        for index, bar in enumerate(score.bars):
            bar.new_system = index in starts and index != 0
            bar.new_page = False
        build()
        if not clearing["signs"] and pdfcheck.sign_on_chord(probe):
            clearing["signs"] = True  # decided before line splits, so a crowded line can split for the room
            build()
        declared = rows(score)
        firsts = {row[0] for row in declared}
        wraps = [n for n in system_starts(probe) if n not in firsts]
        return declared, set(wraps) | crowded_rows(probe, [row[0] for row in declared])

    def trial_bad(trial: set[int]) -> bool:
        nonlocal starts
        kept = starts
        starts = trial
        try:
            return bool(measure()[1])
        finally:
            starts = kept

    if lines is None:
        for _ in range(MAX_LAYOUT_ROUNDS):
            declared, bad = measure()
            if not bad:
                break
            split = False
            for row in declared:
                if any(n in row for n in bad) and len(row) > 1:
                    ease_line(starts, row, fixed, tied)
                    split = True
            if not split:
                raise DoesNotFit(f"{song}: bars {sorted(bad)} do not fit alone")
        else:
            raise DoesNotFit(f"{song}: lines {sorted(bad)} still do not fit")
        refused: set[int] = set()
        for _ in range(MAX_MERGE_ROUNDS):
            joins = merge_candidates(rows(score), fixed, refused, joins_fit)
            if not joins:
                break
            starts -= joins
            declared, bad = measure()
            undone = {at for row in declared if any(n in row for n in bad) for at in joins if row[0] - 1 < at <= row[-1] - 1}
            refused |= undone
            starts |= undone
        if variant == PIANO:
            starts = clear_lone_bars(starts, fixed, len(score.bars), lambda trial: not trial_bad(trial))
        for trial in borrowings(rows(score), fixed):
            if trial[0] in starts and trial[0] - 1 not in starts:
                moved = (starts - {trial[0]}) | {trial[0] - 1}
                if not trial_bad(moved):
                    starts = moved
        for _ in range(MAX_LAYOUT_ROUNDS):  # the last word on the lines is a clean measure, never an ignored one
            declared, bad = measure()
            if not bad:
                break
            for row in declared:
                if any(n in row for n in bad) and len(row) > 1:
                    ease_line(starts, row, fixed, tied)
                elif any(n in row for n in bad):
                    raise DoesNotFit(f"{song}: bar {row[0]} does not fit alone")
        else:
            raise DoesNotFit(f"{song}: lines {sorted(bad)} still do not fit")
    else:
        for _ in range(FINAL_SPLITS):  # seating can crowd a line a trial judged clear
            declared, bad = measure()
            if not bad:
                break
            splitting = [row for row in declared if any(n in row for n in bad) and len(row) > 1]
            if not splitting:
                raise DoesNotFit(f"{song}: bars {sorted(bad)} do not fit alone")
            for row in splitting:
                ease_line(starts, row, fixed, tied)
        else:
            raise DoesNotFit(f"{song}: saved lines no longer fit")
    found = len(pdftext.open(probe))
    if pages is not None and found > pages:
        raise DoesNotFit(f"{song}: {found} pages, over {pages}")
    for _ in range(MAX_LAYOUT_ROUNDS if lines is None else 1):
        if not balance(score, probe):
            break
        build()
        crowded = crowded_rows(probe, [row[0] for row in rows(score)])
        if lines is not None or not crowded:
            break
        for row in rows(score):  # settling the pages can tip a line over; split it and settle again
            if row[0] in crowded and len(row) > 1:
                starts.add(split_at(row[0] - 1, row[-1], tied))
        measure()
    if len(pdftext.open(probe)) != found:
        raise DoesNotFit(f"{song}: even pages need {len(pdftext.open(probe))} pages, not {found}")
    if pdfcheck.sign_on_chord(probe):
        clearing["signs"] = True
        build()
    refuse_fixed(probe, info.title, song)
    if seated:
        try:
            seating = chordseat.problems(base, probe)
        except ValueError as error:
            raise DoesNotFit(f"{song}: {error}") from error
        if seating:
            raise DoesNotFit(f"{song}: {seating[:3]}")
    found_problems = pdfcheck.all_problems(probe, info.title) if seated else pdfcheck.text_problems(probe)
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


def line_starts(score: Score) -> list[int]:
    return [row[0] - 1 for row in rows(score)]


def layout_rules() -> str:
    """A digest of what decides a layout: a saved layout from other rules is searched again."""
    whole = hashlib.sha256()
    for name in LAYOUT_FILES:
        whole.update((pathlib.Path(__file__).parent / name).read_bytes())
    return whole.hexdigest()[:16]


def line_rule_breaks(score: Score) -> list[int]:
    """Bars the line rules say start a line and that sit inside one, or lines opening on a bar without a chord that a mark does not excuse."""
    firsts = {row[0] for row in rows(score)}
    marks = {block[0] + 1 for block in sections(score)}
    inside = [bar.number for bar in score.bars if bar.number not in firsts and piano.label_of(bar) and not (bar.number > 1 and tied_out(score.bars[bar.number - 2]))]
    bare = [n for n in firsts if n not in marks and not score.bars[n - 1].chords]
    return sorted({*inside, *bare})


def chordless_opening(score: Score, base: pathlib.Path) -> list[int]:
    """Lines the built score opens without a chord, with the gate's own reading; a section mark excuses its line."""
    marks = {block[0] + 1 for block in sections(score)}
    return [bar for bar in chordseat.chordless_starts(chordseat.score_text(base)[1]) if bar not in marks]


def prove(src: pathlib.Path, work: pathlib.Path, variant: str, saved: dict[str, Any]) -> tuple[Score, pathlib.Path, float, float, int] | None:
    """Rebuild a saved decision once; None sends the sheet back to the search."""
    try:
        laid, base, probe = lay_out(src, work, float(saved["spatium"]), float(saved["distance"]), int(saved["pages"]), variant, list(saved["lines"]))
    except DoesNotFit:
        return None
    if not acceptable(laid, variant) or line_rule_breaks(laid) or chordless_opening(laid, base):
        return None
    return laid, base, float(saved["spatium"]), float(saved["distance"]), len(pdftext.open(probe))


def acceptable(laid: Score, variant: str, share: float = MAX_SHORT_SHARE) -> bool:
    if variant == PIANO:
        return not piano.lone_lines([row[0] for row in rows(laid)], len(laid.bars))
    return short_line_share(rows(laid)) <= share


TRIALS: dict[tuple[str, str, int | None, float, float], Score | None] = {}
FILLS: dict[tuple[str, str, float, float], float] = {}  # the least filled page of each trial, as a share of its height


def page_fill(pdf: pathlib.Path) -> float:
    """How far down its page the music reaches on the least filled page, as a share of the page height."""
    shares = []
    for page in pdftext.open(pdf):
        pix = page.get_pixmap(dpi=36, colorspace=pymupdf.csGRAY)
        inked = [y for y in range(pix.height) if min(pix.samples[y * pix.width:(y + 1) * pix.width]) < 128]
        shares.append((max(inked) + 1) / pix.height if inked else 0.0)
    return min(shares, default=0.0)


def trials(src: pathlib.Path, work: pathlib.Path, variant: str, limit: int | None, pairs: list[tuple[float, float]]) -> dict[tuple[float, float], Score | None]:
    """Every size and line gap laid out side by side, their renders sharing jobs; None where it does not fit."""
    wanted = [pair for pair in dict.fromkeys(pairs) if (str(src), variant, limit, *pair) not in TRIALS]

    def one(pair: tuple[float, float]) -> tuple[tuple[float, float], Score | None]:
        with BATCHER.worker():
            try:
                laid, _, probe = lay_out(src, work, pair[0], pair[1], limit, variant, seated=False)
                FILLS[(str(src), variant, *pair)] = page_fill(probe)
                return pair, laid
            except DoesNotFit:
                return pair, None

    if wanted:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(wanted)) as pool:
            for pair, laid in pool.map(one, wanted):
                TRIALS[(str(src), variant, limit, *pair)] = laid
    return {pair: TRIALS[(str(src), variant, limit, *pair)] for pair in pairs}


def sizes_between(low: float, high: float) -> list[float]:
    return [round(low + step * SIZE_STEP, 2) for step in range(int(round((high - low) / SIZE_STEP)) + 1)]


def fit(src: pathlib.Path, work: pathlib.Path, variant: str) -> tuple[Score, pathlib.Path, float, float, int] | None:
    """The largest notation up to the cap whose lines fill every page they need, every size laid out side by side; only the chosen 1 is seated."""
    pairs = [(size, BASE_DISTANCE) for size in sizes_between(SIZE_FLOORS[-1], SIZE_CAP)]
    found = trials(src, work, variant, None, pairs)
    chosen = None
    for share in (MAX_SHORT_SHARE, RELAXED_SHORT_SHARE):  # bigger notes outrank fuller lines, within the wider share
        good = [pair for pair, laid in found.items() if laid is not None and acceptable(laid, variant, share)]
        full = [pair for pair in good if FILLS.get((str(src), variant, *pair), 0.0) >= PAGE_FILL]
        chosen = max(full or good, default=None)
        if chosen is not None:
            if share != MAX_SHORT_SHARE:
                print(f"{src.stem} {variant}: more than {MAX_SHORT_SHARE:.0%} short lines allowed for size {chosen[0]}", flush=True)
            break
    if chosen is None:
        return None
    return finish(src, work, variant, None, chosen[0], chosen[1])


def finish(src: pathlib.Path, work: pathlib.Path, variant: str, limit: int | None, spatium: float, distance: float) -> tuple[Score, pathlib.Path, float, float, int] | None:
    """Seat and check the chosen size in full, stepping down a little when it fails; then open the line gap into the height left."""
    SEARCH_CAP["at"] = LAUNCHES["count"] + FINAL_LAUNCH_CAP
    try:
        return finish_within_cap(src, work, variant, limit, spatium, distance)
    finally:
        SEARCH_CAP["at"] = 0


def finish_within_cap(src: pathlib.Path, work: pathlib.Path, variant: str, limit: int | None, spatium: float, distance: float) -> tuple[Score, pathlib.Path, float, float, int] | None:
    """The chosen size and the few below it seated side by side; the largest that passes in full wins."""
    sizes = [size for size in (round(spatium - step * SIZE_STEP, 2) for step in range(FINAL_STEPS)) if size >= SIZE_FLOORS[-1]]

    def one(size: float) -> tuple[Score, pathlib.Path, float, float, int] | None:
        for start_gap in sorted({distance, MIDDLE_DISTANCE, TIGHT_DISTANCE}, reverse=True):  # a tighter line gap keeps the bigger notation
            if start_gap > distance:
                continue
            laid = trials(src, work, variant, limit, [(size, start_gap)])[(size, start_gap)]
            if laid is None:
                continue
            lines = line_starts(laid)
            gaps = [start_gap * 2, start_gap * 1.5, start_gap]  # the line gap changes height alone, so the lines hold at every gap
            widened = held_lines(src, work, variant, limit, size, gaps, lines)
            for gap in [g for g in gaps if widened[g]]:
                with BATCHER.worker():
                    try:
                        score, base, probe = lay_out(src, work, size, gap, limit, variant, lines)
                    except DoesNotFit:
                        continue
                return score, base, size, gap, len(pdftext.open(probe))
        return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(sizes)) as pool:
        done = [found for found in pool.map(one, sizes) if found is not None]
    return max(done, key=lambda found: found[2]) if done else None


def held_lines(src: pathlib.Path, work: pathlib.Path, variant: str, limit: int | None, size: float, gaps: list[float], lines: list[int]) -> dict[float, bool]:
    """Whether the given lines fit their pages at each line gap, the gaps laid out side by side and unseated."""

    def one(gap: float) -> tuple[float, bool]:
        with BATCHER.worker():
            try:
                lay_out(src, work, size, gap, limit, variant, lines, seated=False)
                return gap, True
            except DoesNotFit:
                return gap, False

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(gaps)) as pool:
        return dict(pool.map(one, gaps))


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
    legacy_sign_size(raw)
    chordnames.embed_chord_list(raw, chordnames.chord_file(work))
    seen = work / f"{info.name}-legacy-seat.pdf"

    def render() -> None:
        LAUNCHES["count"] += 1
        for out in (seen, seen.with_suffix(".mpos")):
            subprocess.run([legacy_command(), "-o", str(out), str(raw)], capture_output=True, check=True, timeout=240, stdin=subprocess.DEVNULL)

    render()
    seat_chords(raw, seen, render, f"{info.name}-legacy", {bar.number for bar in score.bars if piano.has_notes(bar)}, chordseat.LEGACY_REACH)
    target.write_bytes(raw.read_bytes())


def legacy_sign_size(mscz: pathlib.Path) -> None:
    """MuseScore 3 sizes a sign as text."""
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
            for spelled, legacy_name in chordnames.LEGACY_SPELLING.items():
                text = text.replace(f"<name>{spelled}</name>", f"<name>{legacy_name}</name>")
            items[name] = text.encode("utf-8")
    with zipfile.ZipFile(mscz, "w", zipfile.ZIP_DEFLATED) as z:
        for key, data in items.items():
            z.writestr(key, data)


def sheet(src: pathlib.Path, work: pathlib.Path, variant: str, saved: dict[str, Any] | None) -> tuple[Score, pathlib.Path, float, float, int]:
    """A saved decision re-proved in 1 pass, or the search; no sheet has a page limit."""
    found = prove(src, work, variant, saved) if saved and not floor_gap_problems(variant, saved) else None  # a finished search stays paid for
    SEARCHED["rules"] = saved.get("searched", saved.get("rules")) if found and saved else layout_rules()
    found = found or fit(src, work, variant)
    if found is None:
        raise DoesNotFit(f"{src.stem}: the {variant} sheet has no passing layout at any size")
    return found


def floor_gap_problems(variant: str, saved: dict[str, Any] | None) -> list[str]:
    """A sheet at the smallest size with room to widen its line gap had height to spend on size, so its search failed."""
    if saved and float(saved["spatium"]) <= SIZE_FLOORS[0] and float(saved["distance"]) > BASE_DISTANCE:
        return [f"{variant} sheet at the size floor {saved['spatium']} with line gap {saved['distance']}"]
    return []


def decision(found: tuple[Score, pathlib.Path, float, float, int]) -> dict[str, Any]:
    return {"rules": layout_rules(), "searched": SEARCHED["rules"], "spatium": found[2], "distance": found[3], "pages": found[4], "lines": line_starts(found[0])}


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

    outputs = [out_dir / info.score_file, out_dir / info.original_pdf] + ([out_dir / info.legacy_file] if LEGACY_FILES else [])
    key, ready = current(ORIGINAL, shared, outputs)
    if not ready:
        found = sheet(src, work, ORIGINAL, old(ORIGINAL))
        (out_dir / info.score_file).write_bytes(found[1].read_bytes())
        shutil.copyfile(found[1].with_name(found[1].stem + "-layout.pdf"), out_dir / info.original_pdf)
        if LEGACY_FILES:
            # the legacy file reads the original tone's MusicXML, so it is built before anything overwrites it
            build_legacy(src, work, out_dir / info.legacy_file, found[2], found[3], info)
            chordnames.drop_chord_path(out_dir / info.score_file)
            chordnames.drop_chord_path(out_dir / info.legacy_file)
            legacy_gap = scorefile.difference(out_dir / info.score_file, out_dir / info.legacy_file)
            report["legacy"] = legacy_gap[:6]
            if legacy_gap:
                (out_dir / info.legacy_file).unlink()
                outputs = outputs[:2]
        else:
            chordnames.drop_chord_path(out_dir / info.score_file)
            (out_dir / info.legacy_file).unlink(missing_ok=True)
        keep(ORIGINAL, key, outputs, found)
    if info.direction:
        tone_score = work / f"{info.name}-{TONE}.mscz"  # the gate reads the tone sheet's own score
        outputs = [out_dir / info.tone_pdf, tone_score]
        key, ready = current(TONE, shared, outputs)
        if not ready:
            found = sheet(src, work, TONE, old(TONE))
            shutil.copyfile(found[1].with_name(found[1].stem + "-layout.pdf"), out_dir / info.tone_pdf)
            shutil.copyfile(found[1], tone_score)
            keep(TONE, key, outputs, found)
    final = work / f"{info.name}-piano-final.mscz"
    outputs = [out_dir / info.piano_pdf, final]
    key, ready = current(PIANO, every, outputs)
    if not ready:
        found = sheet(src, work, PIANO, old(PIANO))
        shutil.copyfile(found[1].with_name(found[1].stem + "-layout.pdf"), out_dir / info.piano_pdf)
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
