"""Seat every beat-1 chord a fixed gap after its barline.

Measured on the rendered PDF, written as MuseScore's own per-chord offset.
"""

from __future__ import annotations

import bisect
import collections
import copy
import itertools
import math
import operator
import re
import statistics
import string
import threading
import weakref
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, NamedTuple, NotRequired, TypedDict

import pymupdf

from muse import mode, pdftext
from muse.smufl import HEAD_CODES, PRIVATE_USE, TIME_SIGNATURES

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Iterable

GAP = 0.0
TOLERANCE = mode.pick(0.4, 2.0)
INK_DPI = 720
POINTS_PER_INCH = 72
MPOS_UNITS = 200.0
DEFAULT_SPACE = 3.4
SIGNATURE_GAP = 2.0
LEDGER_REACH = 6.0
OPENING_REPEAT_REACH = 14.0
INK_STROKE_SHARE = 0.1
STEM_REACH = 1.0
INK_REACH = 0.8
SIGN_INK_WIDTH = 0.75
SIGN_CLEAR = 1.5
SIGN_TOUCH_SLACK = 1.0
SIGN_FRAME = (10.0, 60.0)
TARGET_LABELS = ("segno", "codab")
SIGN_TEXT_OVERLAP = 0.5
SIGN_AT_BARLINE = 4.0
EQUAL_TOLERANCE = mode.pick(1.5, 4.0)
CHORD_GAP = mode.pick(0.75, 0.5)
MIN_STAFF_WIDTH = 60.0
MIN_RULE_WIDTH = 10.0
MIN_STAFF_SPACE = 2.0
MAX_STAFF_SPACE = 9.0
SPACE_SLACK = 0.3
FIRST_ELEMENT = re.compile(r"<Chord>|<Rest>|<location>")
CHORD_SHAPE = re.compile(r"[A-G][#b♯♭]?(?:m|maj|sus|dim|add|aug|\d|\(|/|[Δø°]|$)")
SIGN_CHARS = "\ue047\ue048"
MEASURE = re.compile(r"<Measure[^>]*>.*?</Measure>", re.DOTALL)
CLEF_FIRST = 0xE050
CLEF_LAST = 0xE07F
ACCIDENTAL_FIRST = 0xE260
ACCIDENTAL_LAST = 0xE264
CHORD_SIZE_MATCH = 0.1
STAFF_LINE_COUNT = 5
BARLINE_MAX_WIDTH = 3
MIN_EMPTY_BARS = 2


class LineRecord(TypedDict, total=False):
    """Measured data for one printed line carrying chords."""

    chords: list[float]
    bars: list[float]
    openings: list[float]
    signed: list[float]
    crossings: list[float]
    start: float
    opening: float
    space: float
    rights: list[float]
    lefts: list[float]
    size: float
    crossed: NotRequired[list[tuple[int, float]]]


def score_text(mscz: pathlib.Path) -> tuple[str, str]:
    with zipfile.ZipFile(mscz) as z:
        name = next(n for n in z.namelist() if n.endswith(".mscx") and "/" not in n)
        return name, z.read(name).decode("utf-8")


def rows_of(text: str) -> list[list[tuple[int, bool]]]:
    """Per line: (bar index on the line, on beat 1) of each chord, in print order.

    Returns:
        Per line, (bar index on the line, on beat 1) of each chord, in print order.

    """
    first = text.index('<Staff id="1">', text.index("</Part>"))
    end = text.find('<Staff id="2">', first)
    body = text[first : end if end != -1 else text.index("</Score>", first)]
    rows: list[list[tuple[int, bool]]] = [[]]
    place = 0
    for match in MEASURE.finditer(body):
        measure = match.group(0)
        for chord in re.finditer(r"<Harmony>", measure):
            rows[-1].append((place, not FIRST_ELEMENT.search(measure[: chord.start()])))
        place += 1
        if "<LayoutBreak>" in measure:
            rows.append([])
            place = 0
    return rows


def segments(page: pymupdf.Page | pdftext.Page) -> list[pymupdf.Rect]:
    """Split every drawn stroke on its own: 1 program draws a staff as 5 lines in 1 path, another as 1 path per line.

    Returns:
        Every drawn stroke's box.

    """
    found: list[pymupdf.Rect] = []
    for drawing in page.get_drawings():
        half = (drawing.get("width") or 0.0) / 2
        for item in drawing["items"]:
            if item[0] == "l":
                first, second = item[1], item[2]
                vertical = abs(first.x - second.x) < abs(first.y - second.y)
                found.append(
                    pymupdf.Rect(
                        min(first.x, second.x) - (half if vertical else 0),
                        min(first.y, second.y),
                        max(first.x, second.x) + (half if vertical else 0),
                        max(first.y, second.y),
                    ),
                )
            elif item[0] == "re":
                found.append(pymupdf.Rect(item[1]))
            elif item[0] == "qu":
                found.append(item[1].rect)
    return found


def staves_of(shapes: list[pymupdf.Rect]) -> list[list[float]]:
    """Find the 5 line heights of each staff; a staff line may be drawn in 1 piece per bar, so widths add up per height.

    Returns:
        The 5 line heights of each staff.

    """
    widths: dict[float, float] = {}
    for rect in shapes:
        if rect.width > MIN_RULE_WIDTH and rect.height < 1:
            widths[round(rect.y0, 1)] = widths.get(round(rect.y0, 1), 0.0) + rect.width
    rules = sorted(y for y, width in widths.items() if width > MIN_STAFF_WIDTH)
    staves: list[list[float]] = []
    used: set[float] = set()
    for top in rules:
        if top in used:
            continue
        for guess in (y - top for y in rules if MIN_STAFF_SPACE < y - top < MAX_STAFF_SPACE):
            bottom = next((y for y in rules if abs(y - (top + guess * 4)) < guess / 4), None)
            step = (bottom - top) / 4 if bottom is not None else guess
            five = [next((y for y in rules if abs(y - (top + step * k)) < SPACE_SLACK), None) for k in range(5)]
            if None not in five:
                staves.append([float(y) for y in five if y is not None])
                used.update(staves[-1])
                break
    return staves


INK_DARK = 128
BASE_MATCH = 0.5


def char_boxes(
    page: pymupdf.Page | pdftext.Page,
    clip: pymupdf.Rect,
    base: float,
) -> tuple[list[pymupdf.Rect], list[pymupdf.Rect]]:
    """Inside a window, split the boxes of the chord's own letters from every other character's.

    The chord's own letters sit on its baseline; every other character is a key signature, a mark or another word.

    Returns:
        The boxes of the chord's own letters, and the boxes of every other character.

    """
    own: list[pymupdf.Rect] = []
    foreign: list[pymupdf.Rect] = []
    characters, tops, tallest = _characters(page)
    first = bisect.bisect_left(tops, clip.y0 - tallest)
    last = bisect.bisect_right(tops, clip.y1)
    for box, origin_y, code in characters[first:last]:
        if box.intersects(clip):
            (foreign if abs(origin_y - base) > BASE_MATCH or code in PRIVATE_USE else own).append(box)
    return own, foreign


Characters = tuple[list[tuple[pymupdf.Rect, float, int]], list[float], float]
CHARACTERS: weakref.WeakKeyDictionary[pdftext.Page, Characters] = weakref.WeakKeyDictionary()
STRIPS: weakref.WeakKeyDictionary[pdftext.Page, dict[tuple[float, float], pymupdf.Pixmap]] = weakref.WeakKeyDictionary()
STRIP_LOCK = threading.Lock()


def _characters(page: pymupdf.Page | pdftext.Page) -> Characters:
    """List every character's box, baseline and code by top; shared only for a page of 1 fixed version of a file.

    Returns:
        The page's characters by top, their tops, and the tallest box, so a band's characters are found by bisection.

    """
    if not isinstance(page, pdftext.Page):
        return _read_characters(page)
    with STRIP_LOCK:
        if page not in CHARACTERS:
            CHARACTERS[page] = _read_characters(page)
        return CHARACTERS[page]


def _read_characters(page: pymupdf.Page | pdftext.Page) -> Characters:
    found: list[tuple[pymupdf.Rect, float, int]] = sorted(
        (
            (pymupdf.Rect(c["bbox"]), float(c["origin"][1]), ord(c["c"]))
            for block in page.get_text("rawdict")["blocks"]
            for line in block.get("lines", [])
            for span in line["spans"]
            for c in span["chars"]
        ),
        key=lambda character: character[0].y0,
    )
    tops: list[float] = [float(box.y0) for box, _, _ in found]
    return found, tops, max((float(box.height) for box, _, _ in found), default=0.0)


def _strip(page: pymupdf.Page | pdftext.Page, top: float, bottom: float) -> pymupdf.Pixmap:
    """Render the page's ink across its whole width between 2 heights, once for every chord on that band.

    Returns:
        The rendered band.

    """
    area = pymupdf.Rect(page.rect.x0, top, page.rect.x1, bottom)
    with STRIP_LOCK:
        bands = STRIPS.setdefault(page, {}) if isinstance(page, pdftext.Page) else {}
        if (top, bottom) not in bands:
            bands[top, bottom] = page.get_pixmap(dpi=INK_DPI, clip=area, colorspace=pymupdf.csRGB, alpha=False)
        return bands[top, bottom]


def crowded(page: pymupdf.Page | pdftext.Page, left: float, base: float, size: float) -> bool:
    """Whether another character's box enters a chord's ink window, so its raster ink cannot be trusted.

    Returns:
        True when another character's box enters the chord's ink window.

    """
    return bool(char_boxes(page, ink_window(left, base, size), base)[1])


def ink_window(left: float, base: float, size: float) -> pymupdf.Rect:
    """Where a chord's first ink can lie: its first letter's band, a little left of its text box.

    Returns:
        The window a chord's first ink can lie in.

    """
    return pymupdf.Rect(left - INK_REACH, base - size * 0.75, left + size * 0.7, base - 0.2)


def ink_left(page: pymupdf.Page | pdftext.Page, left: float, base: float, size: float) -> float:
    """Where a letter's ink starts, side bearing excluded.

    Rendered, since the text layer reports only its advance box; ink inside another character's box is not the chord's
    unless it also lies in the chord's own letters.

    Returns:
        Where the letter's ink starts.

    """
    clip = ink_window(left, base, size)
    shot = _strip(page, clip.y0, clip.y1)
    samples, n = shot.samples, shot.n
    first = math.floor((clip.x0 - page.rect.x0) * INK_DPI / POINTS_PER_INCH)
    width = min(math.ceil((clip.x1 - page.rect.x0) * INK_DPI / POINTS_PER_INCH), shot.width) - first
    tall = max(2, int(shot.height * INK_STROKE_SHARE))
    own, foreign = char_boxes(page, clip, base)

    def black(row: int, column: int) -> bool:
        """Judge a pixel as chord ink: a chord prints black, a red or blue sign box is never its ink.

        Returns:
            True when the pixel prints black.

        """
        at = row * shot.stride + (first + column) * n
        return bool(max(samples[at : at + 3]) < INK_DARK)

    heights = [clip.y0 + (row + 0.5) * clip.height / shot.height for row in range(shot.height)]
    for column in range(max(0, -first), width):
        x = page.rect.x0 + (first + column + 0.5) * POINTS_PER_INCH / INK_DPI
        own_here = [(box.y0, box.y1) for box in own if box.x0 <= x <= box.x1]
        foreign_here = [(box.y0, box.y1) for box in foreign if box.x0 <= x <= box.x1]
        inked = 0
        for row, y in enumerate(heights):
            if not black(row, column):
                continue
            if any(y0 <= y <= y1 for y0, y1 in own_here) or not any(y0 <= y <= y1 for y0, y1 in foreign_here):
                inked += 1
        if inked >= tall:
            return float(page.rect.x0 + (first + column) * POINTS_PER_INCH / INK_DPI)
    return left


def chord_size_of(words: Iterable[tuple[float, str]]) -> float | None:
    """Find the size chord symbols print at.

    The most common size of chord-shaped text, never the largest text, which can be a title or a sign.

    Returns:
        The size chord symbols print at, or None when no text is chord-shaped.

    """
    shaped = Counter(round(size, 1) for size, text in words if CHORD_SHAPE.match(text.strip()))
    return shaped.most_common(1)[0][0] if shaped else None


def chord_size(path: pathlib.Path) -> float:
    """Find the chord size of a whole PDF.

    Refuses a PDF with no chord text, since every check built on it would judge nothing.

    Returns:
        The chord size of the whole PDF.

    Raises:
        ValueError: The PDF holds no chord text.

    """
    words = [
        (span["size"], span["text"])
        for page in pdftext.read(path)
        for block in page.get_text("dict")["blocks"]
        for line in block.get("lines", [])
        for span in line["spans"]
        if span["text"].strip() and not any(ord(c) in PRIVATE_USE for c in span["text"])
    ]
    size = chord_size_of(words)
    if size is None:
        msg = f"{path.name}: no chord text to size against"
        raise ValueError(msg)
    return size


def measure_boxes(mpos: pathlib.Path) -> list[dict[str, float]]:
    """Every measure's box in points, from the score program's own position export.

    Returns:
        Every measure's box in points.

    """
    elements = list(ET.parse(mpos).getroot().iter("element"))
    units = MPOS_UNITS
    return [
        {key: float(element.get(tag, "0")) / units for key, tag in (("x", "x"), ("y", "y"), ("w", "sx"))}
        | {"page": float(element.get("page", "0"))}
        for element in elements
    ]


def sign_end(char: pdftext.Char, size: float, frames: list[pymupdf.Rect]) -> float:
    """Where a segno or coda stops taking room.

    The further of the coloured box drawn around it and its printed glyph, 1 definition with the touch check.

    Returns:
        Where the sign stops taking room.

    """
    middle = ((char["bbox"][0] + char["bbox"][2]) / 2, (char["bbox"][1] + char["bbox"][3]) / 2)
    around = [frame.x1 for frame in frames if frame.x0 <= middle[0] <= frame.x1 and frame.y0 <= middle[1] <= frame.y1]
    edge = float(max([*around, sign_ink_end(float(char["origin"][0]), size)]))
    return edge + TOLERANCE + SIGN_CLEAR


def sign_ink_end(x: float, size: float) -> float:
    """Where a sign's printed part ends, as the touch check judges it.

    Returns:
        Where the sign's printed part ends.

    """
    return x + size * SIGN_INK_WIDTH - SIGN_TOUCH_SLACK


def sign_middle(char: pdftext.Char) -> float:
    """Find the middle of a segno or coda glyph.

    A sign belongs to the bar its middle falls in, as its box is drawn around it.

    Returns:
        The middle of the glyph.

    """
    return (float(char["bbox"][0]) + float(char["bbox"][2])) / 2


@dataclass
class _PageMarks:
    page: pymupdf.Page | pdftext.Page
    glyphs: list[tuple[float, float, float, int]]
    signs_here: list[tuple[float, float, float]]
    chords: list[tuple[float, float, float, float, str]]
    shapes: list[pymupdf.Rect]
    systems: list[dict[str, float]]
    tops: list[float]
    space: float
    top: float


def _page_marks(
    page: pymupdf.Page | pdftext.Page,
) -> tuple[
    list[tuple[float, float, float, float, str]],
    list[tuple[float, float, float, int]],
    list[tuple[float, float, float]],
]:
    words: list[tuple[float, float, float, float, str]] = []
    glyphs: list[tuple[float, float, float, int]] = []
    signs_here: list[tuple[float, float, float]] = []
    frames = [
        d["rect"]
        for d in page.get_drawings()
        if (color := d.get("color")) is not None
        and color not in {(), (0.0, 0.0, 0.0)}
        and SIGN_FRAME[0] < d["rect"].width < SIGN_FRAME[1]
    ]
    for block in page.get_text("rawdict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                text = "".join(c["c"] for c in span["chars"]).strip()
                if not text:
                    continue
                if all(ord(c) in PRIVATE_USE for c in text):
                    glyphs += [(c["origin"][0], c["origin"][1], c["bbox"][2], ord(c["c"])) for c in span["chars"]]
                    signs_here += [
                        (sign_end(c, span["size"], frames), c["origin"][1], sign_middle(c))
                        for c in span["chars"]
                        if c["c"] in SIGN_CHARS
                    ]
                else:
                    words.append((span["size"], span["bbox"][0], span["bbox"][2], span["origin"][1], text))
    return words, glyphs, signs_here


def _page_setup(page: pymupdf.Page | pdftext.Page, measures: list[dict[str, float]]) -> _PageMarks | None:
    words, glyphs, signs_here = _page_marks(page)
    if not words:
        return None
    top = chord_size_of((w[0], w[4]) for w in words)
    if top is None:
        return None
    chords = sorted((w for w in words if abs(w[0] - top) < CHORD_SIZE_MATCH), key=operator.itemgetter(3, 1))
    shapes = segments(page)
    systems = [row for row in measures if row["page"] == page.number]
    tops = sorted({row["y"] for row in systems})
    steps = [(staff[4] - staff[0]) / 4 for staff in staves_of(shapes) if len(staff) == STAFF_LINE_COUNT]
    space = steps[0] if steps else DEFAULT_SPACE
    return _PageMarks(page, glyphs, signs_here, chords, shapes, systems, tops, space, top)


class RenderReading(NamedTuple):
    """What reading 1 render gathers across its lines: every chord reading, and each letter and size's ink offset."""

    readings: list[tuple[int, int, str, float, float, bool]]
    bearings: dict[tuple[str, float], float]


def _chord_edges(
    page: pymupdf.Page | pdftext.Page,
    mine: list[tuple[float, float, float, float, str]],
    line_index: int,
    seen: RenderReading,
) -> tuple[list[float], list[float], list[float]]:
    joined: list[float] = []
    rights: list[float] = []
    lefts: list[float] = []
    texts: list[str] = []
    for word in sorted(mine, key=operator.itemgetter(1)):
        if CHORD_SHAPE.match(word[4]) and not (texts and texts[-1].endswith(("/", "("))):
            busy = crowded(page, word[1], word[3], word[0])
            key = (word[4][0], round(word[0], 1))
            if key in seen.bearings:
                joined.append(word[1] + seen.bearings[key] * word[0])
            else:
                joined.append(ink_left(page, word[1], word[3], word[0]))
                if not busy:
                    seen.bearings[key] = (joined[-1] - word[1]) / word[0]
            seen.readings.append((line_index, len(joined) - 1, word[4][0], word[0], word[1], busy))
            lefts.append(word[1])
            rights.append(word[2])
        elif rights and word[4][0] not in string.ascii_uppercase:
            rights[-1] = word[2]
        texts.append(word[4])
    return joined, rights, lefts


def _line_start(
    marks: _PageMarks,
    row: list[dict[str, float]],
    bars: list[float],
    staff_top: float,
    above: float,
) -> tuple[float, float]:
    space = marks.space
    bottom = staff_top + space * 4
    reach = space * LEDGER_REACH
    near = sorted(
        (g for g in marks.glyphs if staff_top - reach <= g[1] <= bottom + reach and g[0] < bars[0]),
        key=operator.itemgetter(0),
    )
    start = max(
        signature_ends(near, [g for g in near if HEAD_CODES[0] <= g[3] <= HEAD_CODES[1]], space),
        default=row[0]["x"],
    )
    heads = [(g[0], g[2]) for g in near if HEAD_CODES[0] <= g[3] <= HEAD_CODES[1]]
    start = max([
        start,
        *(
            float(r.x1)
            for r in marks.shapes
            if r.width < BARLINE_MAX_WIDTH
            and abs(r.y0 - staff_top) < 1
            and r.y1 >= bottom - 1
            and start <= r.x0 < start + OPENING_REPEAT_REACH
            and not is_stem(r.x0, heads)
        ),
    ])
    opening = start
    start = max([
        start,
        *(end for end, y, _ in marks.signs_here if above + space * 4 < y <= staff_top + 1 and end < start + marks.top * 4),
    ])
    return start, opening


def _bar_openings(marks: _PageMarks, bars: list[float], staff_top: float) -> list[float]:
    space = marks.space
    bottom = staff_top + space * 4
    reach = space * LEDGER_REACH
    band = sorted((g for g in marks.glyphs if staff_top - reach <= g[1] <= bottom + reach), key=operator.itemgetter(0))
    return [
        max(
            signature_ends(
                [g for g in band if g[0] > edge],
                [g for g in band if HEAD_CODES[0] <= g[3] <= HEAD_CODES[1]],
                space,
                edge,
            ),
            default=edge,
        )
        for edge in bars[:-1]
    ]


def _sign_room(
    marks: _PageMarks,
    bars: list[float],
    openings: list[float],
    staff_top: float,
    above: float,
) -> tuple[list[float], list[float]]:
    space = marks.space
    in_band = [(end, middle) for end, y, middle in marks.signs_here if above + space * 4 < y <= staff_top + 1]
    signed = [
        max([opened, *(end for end, middle in in_band if edge < middle <= edge + space * SIGN_AT_BARLINE)])
        for edge, opened in zip(bars[:-1], openings, strict=True)
    ]
    crossings = [
        max([
            0.0,
            *(
                end - TOLERANCE - SIGN_CLEAR - edge
                for end, middle in in_band
                if middle < edge < end - TOLERANCE - SIGN_CLEAR
            ),
        ])
        for edge in bars[:-1]
    ]
    return signed, crossings


def _staff_line(
    marks: _PageMarks,
    staff_top: float,
    above: float,
    line_index: int,
    seen: RenderReading,
) -> LineRecord | None:
    mine = [w for w in marks.chords if above + marks.space * 4 < w[3] <= staff_top + 1]
    if not mine:
        return None
    edges = _chord_edges(marks.page, mine, line_index, seen)
    row = sorted((m for m in marks.systems if m["y"] == staff_top), key=operator.itemgetter("x"))
    bars = [m["x"] + m["w"] for m in row]
    start, opening = _line_start(marks, row, bars, staff_top, above)
    openings = _bar_openings(marks, bars, staff_top)
    signed, crossings = _sign_room(marks, bars, openings, staff_top, above)
    return {
        "chords": edges[0],
        "bars": bars,
        "openings": openings,
        "signed": signed,
        "crossings": crossings,
        "start": start,
        "opening": opening,
        "space": marks.space,
        "rights": edges[1],
        "lefts": edges[2],
        "size": marks.top,
    }


def _apply_bearings(
    lines: list[LineRecord],
    readings: list[tuple[int, int, str, float, float, bool]],
) -> None:
    bearings = clean_bearings(lines, readings)
    for line, place, letter, size, left, busy in readings:
        if busy and (letter, round(size, 1)) in bearings:
            lines[line]["chords"][place] = left + bearings[letter, round(size, 1)] * size


LINES_KEPT = 64
LINES: collections.OrderedDict[tuple[str, int, int, int, int], list[LineRecord]] = collections.OrderedDict()
LINES_LOCK = threading.Lock()


def page_lines(pdf: pathlib.Path) -> list[LineRecord]:
    """Every staff carrying chords, in print order, read once per version of the render and its positions.

    Each carries its chord left edges, its barlines, where its signatures end, and its staff space.

    Returns:
        Every staff carrying chords, in print order, as the caller's own copy.

    """
    drawn, placed = pdf.stat(), pdf.with_suffix(".mpos").stat()
    key = (str(pdf), drawn.st_size, drawn.st_mtime_ns, placed.st_size, placed.st_mtime_ns)
    with LINES_LOCK:
        if key in LINES:
            LINES.move_to_end(key)
            return copy.deepcopy(LINES[key])
    found = _read_page_lines(pdf)
    with LINES_LOCK:
        LINES[key] = found
        while len(LINES) > LINES_KEPT:
            LINES.popitem(last=False)
    return copy.deepcopy(found)


def _read_page_lines(pdf: pathlib.Path) -> list[LineRecord]:
    lines: list[LineRecord] = []
    seen = RenderReading([], {})
    measures = measure_boxes(pdf.with_suffix(".mpos"))
    for page in pdftext.read(pdf):
        marks = _page_setup(page, measures)
        if marks is None:
            continue
        for at, staff_top in enumerate(marks.tops):
            above = marks.tops[at - 1] if at else -1000.0
            line = _staff_line(marks, staff_top, above, len(lines), seen)
            if line is not None:
                lines.append(line)
    _apply_bearings(lines, seen.readings)
    return lines


def clean_bearings(
    lines: list[LineRecord],
    readings: list[tuple[int, int, str, float, float, bool]],
) -> dict[tuple[str, float], float]:
    """Per first letter and size, the median ink offset past the text box, in sizes.

    Taken over the chords no other glyph crowds.

    Returns:
        Per first letter and size, the median ink offset past the text box, in sizes.

    """
    found: dict[tuple[str, float], list[float]] = {}
    for line, place, letter, size, left, busy in readings:
        if not busy:
            found.setdefault((letter, round(size, 1)), []).append((lines[line]["chords"][place] - left) / size)
    return {key: statistics.median(values) for key, values in found.items()}


def is_stem(x: float, heads: list[tuple[float, float]]) -> bool:
    """Judge a vertical touching a notehead's edge as its stem, however tall; a barline stands clear of the notes.

    Returns:
        True when the vertical touches a notehead's edge.

    """
    return any(abs(x - left) < STEM_REACH or abs(x - right) < STEM_REACH for left, right in heads)


def signature_ends(
    glyphs: list[tuple[float, float, float, int]],
    heads: list[tuple[float, float, float, int]] | None = None,
    space: float = DEFAULT_SPACE,
    begin: float | None = None,
) -> list[float]:
    """Right edges of the unbroken run of clef, key and time from a line's or bar's start.

    An accidental on a note, which its head follows at once on the same height, is no part of it.

    Returns:
        The right edges of the run.

    """
    ends: list[float] = []
    for glyph in glyphs:
        if not is_signature(glyph[3]):
            break
        if glyph[0] - (ends[-1] if ends else begin if begin is not None else glyph[0]) > space * SIGNATURE_GAP:
            break
        if (
            heads is not None
            and len(heads) > 0
            and ACCIDENTAL_FIRST <= glyph[3] <= ACCIDENTAL_LAST
            and any(0 < head[0] - glyph[2] < space * 1.5 and abs(head[1] - glyph[1]) < space * 0.3 for head in heads)
        ):
            break
        ends.append(glyph[2])
    return ends


def target_bars(text: str) -> set[int]:
    """1-based bars of the first staff holding a segno or coda target, which opens its bar.

    A jump or a to-coda point ends its bar instead.

    Returns:
        The 1-based bars holding a target.

    """
    first = text.index('<Staff id="1">', text.index("</Part>"))
    end = text.find('<Staff id="2">', first)
    body = text[first : end if end != -1 else text.index("</Score>", first)]
    return {
        number
        for number, match in enumerate(MEASURE.finditer(body), 1)
        if any(
            f"<label>{target}</label>" in marker
            for marker in re.findall(r"<Marker>.*?</Marker>", match.group(0), re.DOTALL)
            for target in TARGET_LABELS
        )
    }


def matched(text: str, pdf: pathlib.Path) -> list[LineRecord]:
    """Match the PDF's chord lines to the score, each bar opening past a sign only where the score holds a target there.

    Returns:
        The PDF's chord lines.

    """
    lines = page_lines(pdf)
    targets = target_bars(text)
    chorded = [numbers for numbers, row in zip(bar_rows(text), rows_of(text), strict=False) if row]
    for numbers, line in zip(chorded, lines, strict=False):
        line["openings"] = [
            signed if index + 1 < len(numbers) and numbers[index + 1] in targets else plain
            for index, (plain, signed) in enumerate(zip(line["openings"], line["signed"], strict=True))
        ]
        line["crossed"] = [
            (numbers[index + 1], width)
            for index, width in enumerate(line["crossings"])
            if index + 1 < len(numbers) and numbers[index + 1] not in targets and width > SIGN_TEXT_OVERLAP
        ]
    return lines


def is_signature(code: int) -> bool:
    """Clefs, accidentals of a key signature and time signature digits.

    Returns:
        True when the code is a clef, a key signature accidental or a time signature digit.

    """
    return CLEF_FIRST <= code <= CLEF_LAST or code in TIME_SIGNATURES or ACCIDENTAL_FIRST <= code <= ACCIDENTAL_LAST


def missing(rows: list[list[tuple[int, bool]]], lines: list[LineRecord]) -> list[str]:
    """Chords the score holds and the print lacks, per line: a chord pushed off the page prints nothing.

    Returns:
        A message per line whose chord count differs.

    """
    filled = [row for row in rows if row]
    if len(filled) != len(lines):
        return [f"chords missing from the print: {len(filled)} lines with chords in the score, {len(lines)} in the PDF"]
    return [
        f"chords missing from the print: line {at + 1} holds {len(row)} chords in the score, "
        f"{len(line['chords'])} in the PDF"
        for at, (row, line) in enumerate(zip(filled, lines, strict=True))
        if len(row) != len(line["chords"])
    ]


def gaps(rows: list[list[tuple[int, bool]]], lines: list[LineRecord]) -> list[float | None]:
    """Each chord's gap after its bar's left edge; None for a chord past beat 1, which its own beat places.

    Returns:
        Each chord's gap, or None past beat 1.

    Raises:
        ValueError: The score and the PDF disagree on lines or chords per line.

    """
    filled = [row for row in rows if row]
    if len(filled) != len(lines):
        msg = f"{len(filled)} lines with chords in the score, {len(lines)} in the PDF"
        raise ValueError(msg)
    found: list[float | None] = []
    for row, line in zip(filled, lines, strict=True):
        if len(row) != len(line["chords"]):
            msg = f"a line holds {len(row)} chords in the score, {len(line['chords'])} in the PDF"
            raise ValueError(msg)
        for (place, beat), left in zip(row, line["chords"], strict=True):
            if not beat:
                found.append(None)
                continue
            edge = line["start"] if place == 0 else line["openings"][place - 1]
            found.append(left - edge)
    return found


def space_points(score_path: pathlib.Path) -> float:
    """1 staff space in points, exact, from the score's own style.

    A space measured off the PDF's rounded staff lines errs by a few hundredths.

    Returns:
        1 staff space in points.

    Raises:
        ValueError: The style holds other than 1 staff space.

    """
    with zipfile.ZipFile(score_path) as archive:
        styles = [name for name in archive.namelist() if name.endswith(".mss")]
        found = [re.search(r"<[Ss]patium>([^<]+)</[Ss]patium>", archive.read(name).decode("utf-8")) for name in styles]
    values = [float(match.group(1)) for match in found if match]
    if len(values) != 1:
        msg = f"{score_path.name}: {len(values)} staff spaces in its style, wanted 1"
        raise ValueError(msg)
    return values[0] * 72 / 25.4


def ink_widths(rows: list[list[tuple[int, bool]]], lines: list[LineRecord]) -> list[float]:
    """Each chord's printed width in points, box left to box right as the gap check judges it, in score order.

    A superscript can reach past the text box the score program reports.

    Returns:
        Each chord's printed width in points.

    Raises:
        ValueError: The score and the PDF disagree on chords per line.

    """
    filled = [row for row in rows if row]
    if len(filled) != len(lines) or any(len(row) != len(line["chords"]) for row, line in zip(filled, lines, strict=True)):
        msg = "the score and the PDF disagree on chords per line"
        raise ValueError(msg)
    return [right - left for line in lines for left, right in zip(line["lefts"], line["rights"], strict=True)]


def spaces_of(rows: list[list[tuple[int, bool]]], lines: list[LineRecord]) -> list[float]:
    return [line["space"] for row, line in zip([r for r in rows if r], lines, strict=True) for _ in row]


def set_stretch(mscz: pathlib.Path, factors: dict[int, float]) -> None:
    """Multiply the given bars' stretch."""
    name, text = score_text(mscz)
    first = text.index('<Staff id="1">', text.index("</Part>"))
    end = text.find('<Staff id="2">', first)
    end = end if end != -1 else text.index("</Score>", first)
    number = 0

    def stretch(match: re.Match[str]) -> str:
        nonlocal number
        number += 1
        if number not in factors:
            return match.group(0)
        whole = match.group(0)
        held = re.search(r"<stretch>([\d.]+)</stretch>", whole)
        fresh = f"<stretch>{(float(held.group(1)) if held else 1.0) * factors[number]:.3f}</stretch>"
        if held:
            return whole.replace(held.group(0), fresh, 1)
        tag_end = whole.index(">") + 1
        return whole[:tag_end] + fresh + whole[tag_end:]

    body = MEASURE.sub(stretch, text[first:end])
    with zipfile.ZipFile(mscz) as z:
        items = {info.filename: z.read(info.filename) for info in z.infolist()}
    items[name] = (text[:first] + body + text[end:]).encode("utf-8")
    with zipfile.ZipFile(mscz, "w", zipfile.ZIP_DEFLATED) as z:
        for key, data in items.items():
            z.writestr(key, data)


def bar_rows(text: str) -> list[list[int]]:
    """1-based bar numbers per line, from the score's own line breaks.

    Returns:
        The 1-based bar numbers per line.

    """
    first = text.index('<Staff id="1">', text.index("</Part>"))
    end = text.find('<Staff id="2">', first)
    body = text[first : end if end != -1 else text.index("</Score>", first)]
    rows: list[list[int]] = [[]]
    for number, match in enumerate(MEASURE.finditer(body), 1):
        rows[-1].append(number)
        if "<LayoutBreak>" in match.group(0):
            rows.append([])
    return rows


def widths_of(line: LineRecord) -> list[float]:
    """Laid-out bar widths of a line.

    Its first bar starts after the clef and signatures, never after a sign, which is chord room only.

    Returns:
        The laid-out bar widths.

    """
    edges = [line["opening"], *line["bars"]]
    return [right - left for left, right in itertools.pairwise(edges)]


def signed_bars(text: str) -> set[int]:
    """1-based bars of the score's first staff that carry a segno, coda or jump.

    The sign's own room widens its bar by right.

    Returns:
        The 1-based bars carrying a sign.

    """
    first = text.index('<Staff id="1">', text.index("</Part>"))
    end = text.find('<Staff id="2">', first)
    body = text[first : end if end != -1 else text.index("</Score>", first)]
    return {
        number
        for number, match in enumerate(MEASURE.finditer(body), 1)
        if "<Marker>" in match.group(0) or "<Jump>" in match.group(0)
    }


def empty_lines(text: str, lines: list[LineRecord], noted: set[int]) -> list[list[tuple[int, float]]]:
    """Per line whose barlines match the score, (bar, width) of each bar with no notes and no sign.

    Only where the line holds 2 or more.

    Returns:
        Per such line, (bar, width) of each empty bar.

    """
    marked = noted | signed_bars(text)
    found = []
    chorded = [row for row, chords in zip(bar_rows(text), rows_of(text), strict=True) if chords]
    for row, line in zip(chorded, lines, strict=False):
        widths = widths_of(line)
        if len(widths) != len(row):
            continue
        empty = [(bar, width) for bar, width in zip(row, widths, strict=True) if bar not in marked]
        if len(empty) >= MIN_EMPTY_BARS:
            found.append(empty)
    return found


def empty_widths(text: str, lines: list[LineRecord], noted: set[int]) -> list[tuple[int, float, float]]:
    """(bar, width, the line's mean empty width) for each bar with no notes and no sign.

    Evening and judgement share this 1 definition.

    Returns:
        (bar, width, the line's mean empty width) per empty bar.

    """
    return [
        (bar, width, sum(w for _, w in empty) / len(empty))
        for empty in empty_lines(text, lines, noted)
        for bar, width in empty
    ]


def equalize(mscz: pathlib.Path, pdf: pathlib.Path, noted: set[int]) -> bool:
    """Correct each empty bar's stretch toward the widest empty bar of its line.

    The widest may have been widened by an empty bar's own chords.

    Returns:
        True when anything moved.

    """
    _name, text = score_text(mscz)
    off = {
        bar: widest / width
        for empty in empty_lines(text, matched(text, pdf), noted)
        for widest in [max(w for _, w in empty)]
        for bar, width in empty
        if abs(width - widest) > EQUAL_TOLERANCE / 2
    }
    if not off:
        return False
    set_stretch(mscz, off)
    return True


def uneven(mscz: pathlib.Path, pdf: pathlib.Path, noted: set[int]) -> list[str]:
    """Empty bars of 1 line that differ in width on a final PDF.

    Returns:
        A message per uneven bar.

    """
    return [
        f"bar {bar}: {width:.1f} pt wide, line mean {mean:.1f}"
        for bar, width, mean in empty_widths(score_text(mscz)[1], matched(score_text(mscz)[1], pdf), noted)
        if abs(width - mean) > EQUAL_TOLERANCE
    ]


def chordless_starts(text: str) -> list[int]:
    """Bars opening a line, past the first, that hold no chord of their own.

    Returns:
        The bar numbers.

    """
    found = []
    for numbers, chords in zip(bar_rows(text), rows_of(text), strict=True):
        if numbers and numbers[0] != 1 and not (chords and chords[0][0] == 0):
            found.append(numbers[0])
    return found


def jump_crossings(mscz: pathlib.Path, pdf: pathlib.Path) -> list[tuple[int, float]]:
    """(bar, points) for each sign box reaching past a barline into a bar the score holds no target in.

    Returns:
        (bar, points) per crossing.

    """
    return [crossed for line in matched(score_text(mscz)[1], pdf) for crossed in line["crossed"]]


def problems(mscz: pathlib.Path, pdf: pathlib.Path) -> list[str]:
    """Beat-1 chords off the barline on a final PDF.

    Returns:
        A message per problem.

    """
    text = score_text(mscz)[1]
    rows = rows_of(text)
    lines = matched(text, pdf)
    lost = missing(rows, lines)
    if lost:
        return lost
    found = gaps(rows, lines)
    room = spaces_of(rows, lines)
    crossed = [f"a sign box crosses into bar {bar} by {width:.1f} pt" for line in lines for bar, width in line["crossed"]]
    return crossed + [
        f"chord {index + 1}: gap {gap:.1f} pt, wanted {GAP}"
        for index, (gap, space) in enumerate(zip(found, room, strict=True))
        if gap is not None and abs(gap - GAP) > TOLERANCE
    ]


LINE_TEXT_CLEAR = 2.0


def _spans_between(page: pdftext.Page, upper: float, lower: float) -> list[tuple[str, pymupdf.Rect]]:
    inked = page.get_text("dict", pymupdf.TEXT_ACCURATE_BBOXES)["blocks"]
    return [
        (span["text"].strip(), pymupdf.Rect(span["bbox"]))
        for block in inked
        for line in block.get("lines", [])
        for span in line["spans"]
        if span["text"].strip() and upper < span["bbox"][1] and span["bbox"][3] < lower
    ]


def line_texts(text: str) -> list[tuple[list[str], list[str]]]:
    """Per line of the score, its staff texts above and below the staff, as plain words.

    The score's own placement says which line owns each.

    Returns:
        Per line, its texts above and its texts below.

    """
    first = text.index('<Staff id="1">', text.index("</Part>"))
    end = text.find('<Staff id="2">', first)
    body = text[first : end if end != -1 else text.index("</Score>", first)]
    measures = [match.group(0) for match in MEASURE.finditer(body)]
    found = []
    for row in bar_rows(text):
        above: list[str] = []
        below: list[str] = []
        for number in row:
            for element in re.findall(r"<StaffText>.*?</StaffText>", measures[number - 1], re.DOTALL):
                inner = re.search(r"<text>(.*?)</text>", element, re.DOTALL)
                words = " ".join(re.sub(r"<[^>]+>", " ", inner.group(1)).split()) if inner else ""
                if words:
                    (below if "<placement>below</placement>" in element else above).append(words)
        found.append((above, below))
    return found


def in_cue(words: str, cue: str) -> bool:
    """Whether a printed span's words are whole, consecutive words of a cue.

    A substring test lets 1 letter of a span match any cue that happens to contain it.

    Returns:
        Whether the cue holds the span's words as words.

    """
    span, cue_words = words.split(), cue.split()
    return any(cue_words[at : at + len(span)] == span for at in range(len(cue_words) - len(span) + 1))


def cue_spans(
    spans: list[tuple[str, pymupdf.Rect]],
    below: list[str],
    above: list[str],
) -> tuple[list[pymupdf.Rect], list[tuple[str, pymupdf.Rect]]]:
    """Split the spans between 2 lines into the upper line's cues below it and the lower line's cues above it.

    A span whose words fit a cue of both lines stays in both, so a cue printed on both lines is still found close to
    its twin; the check never compares a span with itself.

    Returns:
        The upper line's cue spans, and the lower line's cue spans with their words.

    """
    mine = [box for words, box in spans if any(in_cue(words, cue) for cue in below)]
    theirs = [(words, box) for words, box in spans if any(in_cue(words, cue) for cue in above)]
    return mine, theirs


def crowded_line_texts(score_path: pathlib.Path, pdf: pathlib.Path) -> list[str]:
    """Text on text between 2 lines.

    A cue the score places below 1 line and a cue it places above the next, sharing a stretch of the page closer than
    the clear distance.

    Returns:
        A message per crowded pair.

    """
    text = score_text(score_path)[1]
    owners = line_texts(text)
    rows = bar_rows(text)
    boxes = measure_boxes(pdf.with_suffix(".mpos"))
    space = space_points(score_path)
    tops = [(int(boxes[row[0] - 1]["page"]), boxes[row[0] - 1]["y"]) for row in rows]
    pages = list(pdftext.read(pdf))
    found = []
    for at in range(len(rows) - 1):
        (page_no, upper), (next_page, lower) = tops[at], tops[at + 1]
        if page_no != next_page:
            continue
        spans = _spans_between(pages[page_no], upper + space * 4, lower)
        mine, theirs = cue_spans(spans, owners[at][1], owners[at + 1][0])
        for words, box in theirs:
            if any(
                other != box
                and other.y0 < box.y0
                and box.x0 < other.x1
                and other.x0 < box.x1
                and box.y0 - other.y1 < space * LINE_TEXT_CLEAR
                for other in mine
            ):
                found.append(f"text on text: {words!r} above line {at + 2} sits too close to a cue below line {at + 1}")
    return found
