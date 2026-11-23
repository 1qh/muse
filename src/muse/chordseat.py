"""Seat every beat-1 chord a fixed gap after its barline: measured on the rendered PDF, written as MuseScore's own per-chord offset."""

from __future__ import annotations

import itertools
import operator
import re
import statistics
import string
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from typing import TYPE_CHECKING, Any

import pymupdf

from muse import mode, pdftext
from muse.smufl import HEAD_CODES

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Iterable

GAP = 0.0
TOLERANCE = mode.pick(0.4, 2.0)
INK_DPI = 720
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


def score_text(mscz: pathlib.Path) -> tuple[str, str]:
    with zipfile.ZipFile(mscz) as z:
        name = next(n for n in z.namelist() if n.endswith(".mscx") and "/" not in n)
        return name, z.read(name).decode("utf-8")


def rows_of(text: str) -> list[list[tuple[int, bool]]]:
    """Per line: (bar index on the line, on beat 1) of each chord, in print order."""
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


def segments(page: pymupdf.Page) -> list[pymupdf.Rect]:
    """Every drawn stroke on its own: 1 program draws a staff as 5 lines in 1 path, another as 1 path per line."""
    found = []
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
    """The 5 line heights of each staff; a staff line may be drawn in 1 piece per bar, so widths add up per height."""
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


def char_boxes(page: pymupdf.Page, clip: pymupdf.Rect, base: float) -> tuple[list[pymupdf.Rect], list[pymupdf.Rect]]:
    """Inside a window, the boxes of the chord's own letters, on its baseline, and of every other character: a key signature, a mark or another word."""
    own: list[pymupdf.Rect] = []
    foreign: list[pymupdf.Rect] = []
    for block in page.get_text("rawdict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                for c in span["chars"]:
                    box = pymupdf.Rect(c["bbox"])
                    if box.intersects(clip):
                        (
                            foreign if abs(c["origin"][1] - base) > BASE_MATCH or 0xE000 <= ord(c["c"]) <= 0xF8FF else own
                        ).append(box)
    return own, foreign


def crowded(page: pymupdf.Page, left: float, base: float, size: float) -> bool:
    """Whether another character's box enters a chord's ink window, so its raster ink cannot be trusted."""
    return bool(char_boxes(page, ink_window(left, base, size), base)[1])


def ink_window(left: float, base: float, size: float) -> pymupdf.Rect:
    """Where a chord's first ink can lie: its first letter's band, a little left of its text box."""
    return pymupdf.Rect(left - INK_REACH, base - size * 0.75, left + size * 0.7, base - 0.2)


def ink_left(page: pymupdf.Page, left: float, base: float, size: float) -> float:
    """Where a letter's ink starts, side bearing excluded: rendered, since the text layer reports only its advance box; ink inside another character's box is not the chord's unless it also lies in the chord's own letters."""
    clip = ink_window(left, base, size)
    shot = page.get_pixmap(dpi=INK_DPI, clip=clip, colorspace=pymupdf.csRGB, alpha=False)
    samples, n = shot.samples, shot.n
    tall = max(2, int(shot.height * INK_STROKE_SHARE))
    own, foreign = char_boxes(page, clip, base)

    def black(row: int, column: int) -> bool:
        """A chord prints black: a red or blue sign box is never its ink."""
        at = row * shot.stride + column * n
        return bool(max(samples[at : at + 3]) < INK_DARK)

    def mine(x: float, y: float) -> bool:
        return any(box.x0 <= x <= box.x1 and box.y0 <= y <= box.y1 for box in own) or not any(
            box.x0 <= x <= box.x1 and box.y0 <= y <= box.y1 for box in foreign
        )

    for column in range(shot.width):
        x = clip.x0 + (column + 0.5) * clip.width / shot.width
        if (
            sum(
                black(row, column) and mine(x, clip.y0 + (row + 0.5) * clip.height / shot.height)
                for row in range(shot.height)
            )
            >= tall
        ):
            return float(clip.x0 + column * clip.width / shot.width)
    return left


def chord_size_of(words: Iterable[tuple[float, str]]) -> float | None:
    """The size chord symbols print at: the most common size of chord-shaped text, never the largest text, which can be a title or a sign."""
    shaped = Counter(round(size, 1) for size, text in words if CHORD_SHAPE.match(text.strip()))
    return shaped.most_common(1)[0][0] if shaped else None


def chord_size(path: pathlib.Path) -> float:
    """The chord size of a whole PDF; refuses a PDF with no chord text, since every check built on it would judge nothing."""
    words = [
        (span["size"], span["text"])
        for page in pdftext.open(path)
        for block in page.get_text("dict")["blocks"]
        for line in block.get("lines", [])
        for span in line["spans"]
        if span["text"].strip() and not any(0xE000 <= ord(c) <= 0xF8FF for c in span["text"])
    ]
    size = chord_size_of(words)
    if size is None:
        msg = f"{path.name}: no chord text to size against"
        raise ValueError(msg)
    return size


def measure_boxes(mpos: pathlib.Path) -> list[dict[str, float]]:
    """Every measure's box in points, from the score program's own position export."""
    elements = list(ET.parse(mpos).getroot().iter("element"))
    units = MPOS_UNITS
    return [
        {key: float(element.get(tag, "0")) / units for key, tag in (("x", "x"), ("y", "y"), ("w", "sx"))}
        | {"page": float(element.get("page", "0"))}
        for element in elements
    ]


def sign_end(char: dict[str, Any], size: float, frames: list[pymupdf.Rect]) -> float:
    """Where a segno or coda stops taking room: the further of the coloured box drawn around it and its printed glyph, 1 definition with the touch check."""
    middle = ((char["bbox"][0] + char["bbox"][2]) / 2, (char["bbox"][1] + char["bbox"][3]) / 2)
    around = [frame.x1 for frame in frames if frame.x0 <= middle[0] <= frame.x1 and frame.y0 <= middle[1] <= frame.y1]
    edge = float(max([*around, sign_ink_end(float(char["origin"][0]), size)]))
    return edge + TOLERANCE + SIGN_CLEAR


def sign_ink_end(x: float, size: float) -> float:
    """Where a sign's printed part ends, as the touch check judges it."""
    return x + size * SIGN_INK_WIDTH - SIGN_TOUCH_SLACK


def sign_middle(char: dict[str, Any]) -> float:
    """The middle of a segno or coda glyph: a sign belongs to the bar its middle falls in, as its box is drawn around it."""
    return (float(char["bbox"][0]) + float(char["bbox"][2])) / 2


def page_lines(pdf: pathlib.Path) -> list[dict[str, Any]]:
    """Every staff carrying chords, in print order: its chord left edges, its barlines, where its signatures end, and its staff space."""
    lines: list[dict[str, Any]] = []
    readings: list[tuple[int, int, str, float, float, bool]] = []
    measures = measure_boxes(pdf.with_suffix(".mpos"))
    for page in pdftext.open(pdf):
        words, glyphs = [], []
        signs_here: list[tuple[float, float, float]] = []
        frames = [
            d["rect"]
            for d in page.get_drawings()
            if d.get("color") and tuple(d["color"]) != (0.0, 0.0, 0.0) and SIGN_FRAME[0] < d["rect"].width < SIGN_FRAME[1]
        ]
        for block in page.get_text("rawdict")["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    text = "".join(c["c"] for c in span["chars"]).strip()
                    if not text:
                        continue
                    if all(0xE000 <= ord(c) <= 0xF8FF for c in text):
                        glyphs += [(c["origin"][0], c["origin"][1], c["bbox"][2], ord(c["c"])) for c in span["chars"]]
                        signs_here += [
                            (sign_end(c, span["size"], frames), c["origin"][1], sign_middle(c))
                            for c in span["chars"]
                            if c["c"] in SIGN_CHARS
                        ]
                    else:
                        words.append((span["size"], span["bbox"][0], span["bbox"][2], span["origin"][1], text))
        if not words:
            continue
        top = chord_size_of((w[0], w[4]) for w in words)
        if top is None:
            continue
        chords = sorted((w for w in words if abs(w[0] - top) < 0.1), key=operator.itemgetter(3, 1))
        shapes = segments(page)
        systems = [row for row in measures if row["page"] == page.number]
        tops = sorted({row["y"] for row in systems})
        steps = [(staff[4] - staff[0]) / 4 for staff in staves_of(shapes) if len(staff) == 5]
        space = steps[0] if steps else DEFAULT_SPACE
        for at, staff_top in enumerate(tops):
            above = tops[at - 1] if at else -1000.0
            mine = [w for w in chords if above + space * 4 < w[3] <= staff_top + 1]
            if not mine:
                continue
            joined: list[float] = []
            rights: list[float] = []
            lefts: list[float] = []
            texts: list[str] = []
            for word in sorted(mine, key=operator.itemgetter(1)):
                if CHORD_SHAPE.match(word[4]) and not (texts and texts[-1].endswith(("/", "("))):
                    joined.append(ink_left(page, word[1], word[3], word[0]))
                    readings.append((
                        len(lines),
                        len(joined) - 1,
                        word[4][0],
                        word[0],
                        word[1],
                        crowded(page, word[1], word[3], word[0]),
                    ))
                    lefts.append(word[1])
                    rights.append(word[2])
                elif rights and word[4][0] not in string.ascii_uppercase:
                    rights[-1] = word[2]
                texts.append(word[4])
            row = sorted((m for m in systems if m["y"] == staff_top), key=operator.itemgetter("x"))
            bars = [m["x"] + m["w"] for m in row]
            bottom = staff_top + space * 4
            reach = space * LEDGER_REACH
            near = sorted(
                (g for g in glyphs if staff_top - reach <= g[1] <= bottom + reach and g[0] < bars[0]),
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
                    r.x1
                    for r in shapes
                    if r.width < 3
                    and abs(r.y0 - staff_top) < 1
                    and r.y1 >= bottom - 1
                    and start <= r.x0 < start + OPENING_REPEAT_REACH
                    and not is_stem(r.x0, heads)
                ),
            ])
            opening = start
            start = max([
                start,
                *(end for end, y, _ in signs_here if above + space * 4 < y <= staff_top + 1 and end < start + top * 4),
            ])
            band = sorted((g for g in glyphs if staff_top - reach <= g[1] <= bottom + reach), key=operator.itemgetter(0))
            openings = [
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
            in_band = [(end, middle) for end, y, middle in signs_here if above + space * 4 < y <= staff_top + 1]
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
            lines.append({
                "chords": joined,
                "bars": bars,
                "openings": openings,
                "signed": signed,
                "crossings": crossings,
                "start": start,
                "opening": opening,
                "space": space,
                "rights": rights,
                "lefts": lefts,
                "size": top,
            })
    bearings = clean_bearings(lines, readings)
    for line, place, letter, size, left, busy in readings:
        if busy and (letter, round(size, 1)) in bearings:
            lines[line]["chords"][place] = left + bearings[letter, round(size, 1)] * size
    return lines


def clean_bearings(
    lines: list[dict[str, Any]],
    readings: list[tuple[int, int, str, float, float, bool]],
) -> dict[tuple[str, float], float]:
    """Per first letter and size, the median ink offset past the text box, in sizes, over the chords no other glyph crowds."""
    found: dict[tuple[str, float], list[float]] = {}
    for line, place, letter, size, left, busy in readings:
        if not busy:
            found.setdefault((letter, round(size, 1)), []).append((lines[line]["chords"][place] - left) / size)
    return {key: statistics.median(values) for key, values in found.items()}


def is_stem(x: float, heads: list[tuple[float, float]]) -> bool:
    """A vertical touching a notehead's edge is its stem, however tall; a barline stands clear of the notes."""
    return any(abs(x - left) < STEM_REACH or abs(x - right) < STEM_REACH for left, right in heads)


def signature_ends(
    glyphs: list[tuple[float, float, float, int]],
    heads: list[tuple[float, float, float, int]] | None = None,
    space: float = DEFAULT_SPACE,
    begin: float | None = None,
) -> list[float]:
    """Right edges of the unbroken run of clef, key and time from a line's or bar's start; an accidental on a note, which its head follows at once on the same height, is no part of it."""
    ends: list[float] = []
    for glyph in glyphs:
        if not is_signature(glyph[3]):
            break
        if glyph[0] - (ends[-1] if ends else begin if begin is not None else glyph[0]) > space * SIGNATURE_GAP:
            break
        if (
            heads
            and 0xE260 <= glyph[3] <= 0xE264
            and any(0 < head[0] - glyph[2] < space * 1.5 and abs(head[1] - glyph[1]) < space * 0.3 for head in heads)
        ):
            break
        ends.append(glyph[2])
    return ends


def target_bars(text: str) -> set[int]:
    """1-based bars of the first staff holding a segno or coda target, which opens its bar; a jump or a to-coda point ends its bar instead."""
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


def matched(text: str, pdf: pathlib.Path) -> list[dict[str, Any]]:
    """The PDF's chord lines, each bar opening past a sign only where the score holds a target there."""
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
    """Clefs, accidentals of a key signature and time signature digits."""
    return 0xE050 <= code <= 0xE07F or 0xE080 <= code <= 0xE09F or 0xE260 <= code <= 0xE264


def missing(rows: list[list[tuple[int, bool]]], lines: list[dict[str, Any]]) -> list[str]:
    """Chords the score holds and the print lacks, per line: a chord pushed off the page prints nothing."""
    filled = [row for row in rows if row]
    if len(filled) != len(lines):
        return [f"chords missing from the print: {len(filled)} lines with chords in the score, {len(lines)} in the PDF"]
    return [
        f"chords missing from the print: line {at + 1} holds {len(row)} chords in the score, {len(line['chords'])} in the PDF"
        for at, (row, line) in enumerate(zip(filled, lines, strict=True))
        if len(row) != len(line["chords"])
    ]


def gaps(rows: list[list[tuple[int, bool]]], lines: list[dict[str, Any]]) -> list[float | None]:
    """Each chord's gap after its bar's left edge; None for a chord past beat 1, which its own beat places."""
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
    """1 staff space in points, exact, from the score's own style: a space measured off the PDF's rounded staff lines errs by a few hundredths."""
    with zipfile.ZipFile(score_path) as archive:
        styles = [name for name in archive.namelist() if name.endswith(".mss")]
        found = [re.search(r"<[Ss]patium>([^<]+)</[Ss]patium>", archive.read(name).decode("utf-8")) for name in styles]
    values = [float(match.group(1)) for match in found if match]
    if len(values) != 1:
        msg = f"{score_path.name}: {len(values)} staff spaces in its style, wanted 1"
        raise ValueError(msg)
    return values[0] * 72 / 25.4


def ink_widths(rows: list[list[tuple[int, bool]]], lines: list[dict[str, Any]]) -> list[float]:
    """Each chord's printed width in points, box left to box right as the gap check judges it, in score order: a superscript can reach past the text box the score program reports."""
    filled = [row for row in rows if row]
    if len(filled) != len(lines) or any(len(row) != len(line["chords"]) for row, line in zip(filled, lines, strict=True)):
        msg = "the score and the PDF disagree on chords per line"
        raise ValueError(msg)
    return [right - left for line in lines for left, right in zip(line["lefts"], line["rights"], strict=True)]


def spaces_of(rows: list[list[tuple[int, bool]]], lines: list[dict[str, Any]]) -> list[float]:
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
    """1-based bar numbers per line, from the score's own line breaks."""
    first = text.index('<Staff id="1">', text.index("</Part>"))
    end = text.find('<Staff id="2">', first)
    body = text[first : end if end != -1 else text.index("</Score>", first)]
    rows: list[list[int]] = [[]]
    for number, match in enumerate(MEASURE.finditer(body), 1):
        rows[-1].append(number)
        if "<LayoutBreak>" in match.group(0):
            rows.append([])
    return rows


def widths_of(line: dict[str, Any]) -> list[float]:
    """Laid-out bar widths of a line; its first bar starts after the clef and signatures, never after a sign, which is chord room only."""
    edges = [line["opening"], *line["bars"]]
    return [right - left for left, right in itertools.pairwise(edges)]


def signed_bars(text: str) -> set[int]:
    """1-based bars of the score's first staff that carry a segno, coda or jump: the sign's own room widens its bar by right."""
    first = text.index('<Staff id="1">', text.index("</Part>"))
    end = text.find('<Staff id="2">', first)
    body = text[first : end if end != -1 else text.index("</Score>", first)]
    return {
        number
        for number, match in enumerate(MEASURE.finditer(body), 1)
        if "<Marker>" in match.group(0) or "<Jump>" in match.group(0)
    }


def empty_lines(text: str, lines: list[dict[str, Any]], noted: set[int]) -> list[list[tuple[int, float]]]:
    """Per line whose barlines match the score, (bar, width) of each bar with no notes and no sign, where it holds 2 or more."""
    noted |= signed_bars(text)
    found = []
    chorded = [row for row, chords in zip(bar_rows(text), rows_of(text), strict=True) if chords]
    for row, line in zip(chorded, lines, strict=False):
        widths = widths_of(line)
        if len(widths) != len(row):
            continue
        empty = [(bar, width) for bar, width in zip(row, widths, strict=True) if bar not in noted]
        if len(empty) >= 2:
            found.append(empty)
    return found


def empty_widths(text: str, lines: list[dict[str, Any]], noted: set[int]) -> list[tuple[int, float, float]]:
    """(bar, width, the line's mean empty width) for each bar with no notes and no sign: evening and judgement share this 1 definition."""
    return [
        (bar, width, sum(w for _, w in empty) / len(empty))
        for empty in empty_lines(text, lines, noted)
        for bar, width in empty
    ]


def equalize(mscz: pathlib.Path, pdf: pathlib.Path, noted: set[int]) -> bool:
    """Correct each empty bar's stretch toward the widest empty bar of its line, which an empty bar's own chords may have widened; True when anything moved."""
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
    """Empty bars of 1 line that differ in width on a final PDF."""
    return [
        f"bar {bar}: {width:.1f} pt wide, line mean {mean:.1f}"
        for bar, width, mean in empty_widths(score_text(mscz)[1], matched(score_text(mscz)[1], pdf), noted)
        if abs(width - mean) > EQUAL_TOLERANCE
    ]


def chordless_starts(text: str) -> list[int]:
    """Bars opening a line, past the first, that hold no chord of their own."""
    found = []
    for numbers, chords in zip(bar_rows(text), rows_of(text), strict=True):
        if numbers and numbers[0] != 1 and not (chords and chords[0][0] == 0):
            found.append(numbers[0])
    return found


def jump_crossings(mscz: pathlib.Path, pdf: pathlib.Path) -> list[tuple[int, float]]:
    """(bar, points) for each sign box reaching past a barline into a bar the score holds no target in."""
    return [crossed for line in matched(score_text(mscz)[1], pdf) for crossed in line["crossed"]]


def problems(mscz: pathlib.Path, pdf: pathlib.Path) -> list[str]:
    """Beat-1 chords off the barline on a final PDF."""
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


def line_texts(text: str) -> list[tuple[list[str], list[str]]]:
    """Per line of the score, its staff texts above and below the staff, as plain words: the score's own placement says which line owns each."""
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


def crowded_line_texts(score_path: pathlib.Path, pdf: pathlib.Path) -> list[str]:
    """Text on text between 2 lines: a cue the score places below 1 line and a cue it places above the next, sharing a stretch of the page closer than the clear distance."""
    text = score_text(score_path)[1]
    owners = line_texts(text)
    rows = bar_rows(text)
    boxes = measure_boxes(pdf.with_suffix(".mpos"))
    space = space_points(score_path)
    tops = [(int(boxes[row[0] - 1]["page"]), boxes[row[0] - 1]["y"]) for row in rows]
    pages = list(pdftext.open(pdf))
    found = []
    for at in range(len(rows) - 1):
        (page_no, upper), (next_page, lower) = tops[at], tops[at + 1]
        if page_no != next_page:
            continue
        inked = pages[page_no].get_text("dict", pymupdf.TEXT_ACCURATE_BBOXES)["blocks"]
        spans = [
            (span["text"].strip(), pymupdf.Rect(span["bbox"]))
            for block in inked
            for line in block.get("lines", [])
            for span in line["spans"]
            if span["text"].strip() and upper + space * 4 < span["bbox"][1] and span["bbox"][3] < lower
        ]
        mine = [box for words, box in spans if any(words in cue for cue in owners[at][1])]
        theirs = [(words, box) for words, box in spans if any(words in cue for cue in owners[at + 1][0])]
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
