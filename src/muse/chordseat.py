"""Seat every beat-1 chord a fixed gap after its barline: measured on the rendered PDF, written as MuseScore's own per-chord offset."""
from __future__ import annotations

import pathlib
import re
import zipfile
from collections import Counter
from typing import Any

import pymupdf

GAP = 0.0  # a beat-1 chord's ink starts exactly at the barline's right edge, or after a line's clef and signatures
TOLERANCE = 0.4
INK_DPI = 720
INK_REACH = 0.8  # points a letter's ink can start left of its text box; further left is a neighbour
SIGN_INK_WIDTH = 0.75  # the printed part of a segno or coda, in sign font sizes
SIGN_REACH = 0.5  # of the chord size: a sign this near the chord baseline shares its row
EQUAL_TOLERANCE = 1.5  # points an empty bar may differ from its line's mean
SAME_BARLINE = 6.0  # the 2 strokes of a repeat or a double bar are 1 barline
MIN_STAFF_WIDTH = 200.0
MIN_RULE_WIDTH = 10.0
MIN_STAFF_SPACE = 2.0
MAX_STAFF_SPACE = 9.0
SPACE_SLACK = 0.3
CHORD_REACH = 40.0
FIRST_ELEMENT = re.compile(r"<Chord>|<Rest>|<location>")
CHORD_SHAPE = re.compile(r"[A-G][#b♯♭]?(?:m|maj|sus|dim|add|aug|\d|\(|/|$)")
SIGN_CHARS = "\ue047\ue048"
MEASURE = re.compile(r"<Measure[^>]*>.*?</Measure>", re.S)


def score_text(mscz: pathlib.Path) -> tuple[str, str]:
    with zipfile.ZipFile(mscz) as z:
        name = next(n for n in z.namelist() if n.endswith(".mscx") and "/" not in n)
        return name, z.read(name).decode("utf-8")


def layout_of(text: str) -> list[list[bool]]:
    """Per line, per chord in order, whether it opens its line's bar on beat 1, with the bar's place on the line: flags only."""
    return [[beat for _, beat in row] for row in rows_of(text)]


def rows_of(text: str) -> list[list[tuple[int, bool]]]:
    """Per line: (bar index on the line, on beat 1) of each chord, in print order."""
    first = text.index('<Staff id="1">', text.index("</Part>"))
    end = text.find('<Staff id="2">', first)
    body = text[first:end if end != -1 else text.index("</Score>", first)]
    rows: list[list[tuple[int, bool]]] = [[]]
    place = 0
    for match in MEASURE.finditer(body):
        measure = match.group(0)
        for chord in re.finditer(r"<Harmony>", measure):
            rows[-1].append((place, not FIRST_ELEMENT.search(measure[:chord.start()])))
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
                found.append(pymupdf.Rect(min(first.x, second.x) - (half if vertical else 0), min(first.y, second.y), max(first.x, second.x) + (half if vertical else 0), max(first.y, second.y)))
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
    staves = []
    at = 0
    while at + 4 < len(rules):
        five = rules[at:at + 5]
        steps = [below - above for above, below in zip(five, five[1:], strict=False)]
        if MIN_STAFF_SPACE < steps[0] < MAX_STAFF_SPACE and all(abs(step - steps[0]) < SPACE_SLACK for step in steps):
            staves.append(five)
            at += 5
        else:
            at += 1  # a page frame or a long stray line is no staff line
    return staves


def ink_left(page: pymupdf.Page, left: float, base: float, size: float) -> float:
    """Where a letter's ink starts, side bearing excluded: rendered, since the text layer reports only its advance box."""
    clip = pymupdf.Rect(left - INK_REACH, base - size * 0.75, left + size * 0.7, base - 0.2)
    shot = page.get_pixmap(dpi=INK_DPI, clip=clip, colorspace=pymupdf.csGRAY)
    samples = shot.samples
    for column in range(shot.width):
        if any(samples[row * shot.stride + column] < 128 for row in range(shot.height)):
            return float(clip.x0 + column * clip.width / shot.width)
    return left


def page_lines(pdf: pathlib.Path) -> list[dict[str, Any]]:
    """Every staff carrying chords, in print order: its chord left edges, its barlines, where its signatures end, and its staff space."""
    lines = []
    for page in pymupdf.open(pdf):
        words, glyphs = [], []
        signs_here: list[tuple[float, float]] = []
        for block in page.get_text("rawdict")["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    text = "".join(c["c"] for c in span["chars"]).strip()
                    if not text:
                        continue
                    if all(0xE000 <= ord(c) <= 0xF8FF for c in text):
                        glyphs += [(c["origin"][0], c["origin"][1], c["bbox"][2], ord(c["c"])) for c in span["chars"]]
                        signs_here += [(c["origin"][0] + span["size"] * SIGN_INK_WIDTH, c["origin"][1]) for c in span["chars"] if c["c"] in SIGN_CHARS]
                    else:
                        words.append((span["size"], span["bbox"][0], span["bbox"][2], span["origin"][1], text))
        if not words:
            continue
        shaped = Counter(round(w[0], 1) for w in words if CHORD_SHAPE.match(w[4]))
        if not shaped:
            continue
        top = shaped.most_common(1)[0][0]  # the title can print larger in 1 program, so the size chords use decides
        chords = sorted((w for w in words if abs(w[0] - top) < 0.1), key=lambda w: (w[3], w[1]))
        shapes = segments(page)
        staves = staves_of(shapes)
        for staff in staves:
            mine = [w for w in chords if staff[0] - CHORD_REACH <= w[3] <= staff[0] + 1 and not any(other[0] < staff[0] and other[0] > w[3] for other in staves)]
            if not mine:
                continue
            joined: list[float] = []
            for word in sorted(mine, key=lambda w: w[1]):
                if word[4][0] in "ABCDEFG":  # a chord's later pieces, such as what follows a flat, name no root
                    joined.append(ink_left(page, word[1], word[3], word[0]))
            left_edge = min(r.x0 for r in shapes if r.width > MIN_RULE_WIDTH and r.height < 1 and abs(r.y0 - staff[0]) < 1)
            bars: list[float] = []
            for x in sorted(r.x1 for r in shapes if r.width < 3 and abs(r.y0 - staff[0]) < 1 and r.y1 >= staff[4] - 1 and r.x0 > left_edge + 2):
                if bars and x - bars[-1] < SAME_BARLINE:
                    bars[-1] = x
                else:
                    bars.append(x)
            signs = [g[2] for g in glyphs if staff[0] - 8 <= g[1] <= staff[4] + 8 and g[0] < (bars[0] if bars else left_edge + 80) and is_signature(g[3])]
            start = max(signs, default=left_edge)
            base = mine[0][3]
            # a sign printed on the chord row takes the place right after the clef, so the chord follows it
            start = max([start, *(end for end, y in signs_here if abs(y - base) < top * SIGN_REACH and end < start + top * 2)])
            lines.append({"chords": joined, "bars": bars, "start": start, "space": (staff[4] - staff[0]) / 4})
    return lines


def matched(text: str, pdf: pathlib.Path) -> list[dict[str, Any]]:
    """The PDF's chord lines, with a repeat barline that opens a line taken as the line's start rather than as a bar."""
    lines = page_lines(pdf)
    chorded = [row for row, chords in zip(bar_rows(text), rows_of(text), strict=True) if chords]
    for row, line in zip(chorded, lines, strict=False):
        if len(line["bars"]) == len(row) + 1:
            line["start"] = line["bars"].pop(0)
    return lines


def is_signature(code: int) -> bool:
    """Clefs, accidentals of a key signature and time signature digits."""
    return 0xE050 <= code <= 0xE07F or 0xE080 <= code <= 0xE09F or 0xE260 <= code <= 0xE264


def gaps(rows: list[list[tuple[int, bool]]], lines: list[dict[str, Any]]) -> list[float | None]:
    """Each chord's gap after its bar's left edge; None for a chord past beat 1, which its own beat places."""
    filled = [row for row in rows if row]
    if len(filled) != len(lines):
        raise ValueError(f"{len(filled)} lines with chords in the score, {len(lines)} in the PDF")
    found: list[float | None] = []
    for row, line in zip(filled, lines, strict=True):
        if len(row) != len(line["chords"]):
            raise ValueError(f"a line holds {len(row)} chords in the score, {len(line['chords'])} in the PDF")
        for (place, beat), left in zip(row, line["chords"], strict=True):
            if not beat:
                found.append(None)
                continue
            edge = line["start"] if place == 0 else line["bars"][place - 1]
            found.append(left - edge)
    return found


def spaces_of(rows: list[list[tuple[int, bool]]], lines: list[dict[str, Any]]) -> list[float]:
    return [line["space"] for row, line in zip([r for r in rows if r], lines, strict=True) for _ in row]


def moved_chord(element: str, spaces: float, free_height: float | None = None) -> str:
    """A chord moved sideways; given a height, it also leaves automatic placement, which holds a chord off a barline in MuseScore 3."""
    old = re.search(r'<offset x="([-\d.e]+)" y="[-\d.e]+"/>', element)
    was = float(old.group(1)) if old else 0.0
    inner = re.sub(r'<offset x="[-\d.e]+" y="[-\d.e]+"/>|<autoplace>0</autoplace>', "", element[len("<Harmony>"):])
    held = "" if free_height is None else "<autoplace>0</autoplace>"
    return f'<Harmony>{held}<offset x="{was + spaces:.3f}" y="{0 if free_height is None else free_height}"/>{inner}'


def align(mscz: pathlib.Path, pdf: pathlib.Path, free_height: float | None = None) -> bool:
    """Move each beat-1 chord to the fixed gap; True when anything moved."""
    name, text = score_text(mscz)
    rows = rows_of(text)
    lines = matched(text, pdf)
    found = gaps(rows, lines)
    room = spaces_of(rows, lines)
    first = text.index('<Staff id="1">', text.index("</Part>"))
    end = text.find('<Staff id="2">', first)
    end = end if end != -1 else text.index("</Score>", first)
    places = [place for row in rows if row for place, _ in row]
    at = iter(zip(found, room, places, strict=True))
    moved = False

    def seat(match: re.Match[str]) -> str:
        nonlocal moved
        gap, space, place = next(at)
        if gap is None or abs(gap - GAP) <= TOLERANCE / 2:
            return match.group(0)
        moved = True
        # a line's first chord stays placed automatically, so section marks and cues stack around it
        return moved_chord(match.group(0), (GAP - gap) / space, free_height if place else None)

    body = re.sub(r"<Harmony>.*?</Harmony>", seat, text[first:end], flags=re.S)
    with zipfile.ZipFile(mscz) as z:
        items = {info.filename: z.read(info.filename) for info in z.infolist()}
    items[name] = (text[:first] + body + text[end:]).encode("utf-8")
    with zipfile.ZipFile(mscz, "w", zipfile.ZIP_DEFLATED) as z:
        for key, data in items.items():
            z.writestr(key, data)
    return moved


def bar_rows(text: str) -> list[list[int]]:
    """1-based bar numbers per line, from the score's own line breaks."""
    first = text.index('<Staff id="1">', text.index("</Part>"))
    end = text.find('<Staff id="2">', first)
    body = text[first:end if end != -1 else text.index("</Score>", first)]
    rows: list[list[int]] = [[]]
    for number, match in enumerate(MEASURE.finditer(body), 1):
        rows[-1].append(number)
        if "<LayoutBreak>" in match.group(0):
            rows.append([])
    return rows


def widths_of(line: dict[str, Any]) -> list[float]:
    """Laid-out bar widths of a line; its first bar starts after the clef and signatures."""
    edges = [line["start"], *line["bars"]]
    return [right - left for left, right in zip(edges, edges[1:], strict=False)]


def empty_widths(text: str, lines: list[dict[str, Any]], noted: set[int]) -> list[tuple[int, float, float]]:
    """(bar, width, the line's mean empty width) for each bar with no notes, on lines whose barlines match the score."""
    found = []
    chorded = [row for row, chords in zip(bar_rows(text), rows_of(text), strict=True) if chords]
    for row, line in zip(chorded, lines, strict=False):
        widths = widths_of(line)
        if len(widths) != len(row):
            continue
        empty = [(bar, width) for bar, width in zip(row, widths, strict=True) if bar not in noted]
        if len(empty) < 2:
            continue
        mean = sum(width for _, width in empty) / len(empty)
        found += [(bar, width, mean) for bar, width in empty]
    return found


def equalize(mscz: pathlib.Path, pdf: pathlib.Path, noted: set[int]) -> bool:
    """Correct each empty bar's stretch by its measured width; True when anything moved."""
    name, text = score_text(mscz)
    off = {bar: mean / width for bar, width, mean in empty_widths(text, matched(text, pdf), noted) if abs(width - mean) > EQUAL_TOLERANCE / 2}
    if not off:
        return False
    first = text.index('<Staff id="1">', text.index("</Part>"))
    end = text.find('<Staff id="2">', first)
    end = end if end != -1 else text.index("</Score>", first)
    number = 0

    def stretch(match: re.Match[str]) -> str:
        nonlocal number
        number += 1
        if number not in off:
            return match.group(0)
        whole = match.group(0)
        held = re.search(r"<stretch>([\d.]+)</stretch>", whole)
        fresh = f"<stretch>{(float(held.group(1)) if held else 1.0) * off[number]:.3f}</stretch>"
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
    return True


def uneven(mscz: pathlib.Path, pdf: pathlib.Path, noted: set[int]) -> list[str]:
    """Empty bars of 1 line that differ in width on a final PDF."""
    return [f"bar {bar}: {width:.1f} pt wide, line mean {mean:.1f}" for bar, width, mean in empty_widths(score_text(mscz)[1], matched(score_text(mscz)[1], pdf), noted) if abs(width - mean) > EQUAL_TOLERANCE]


def problems(mscz: pathlib.Path, pdf: pathlib.Path) -> list[str]:
    """Beat-1 chords off the fixed gap on a final PDF."""
    text = score_text(mscz)[1]
    found = gaps(rows_of(text), matched(text, pdf))
    return [f"chord {index + 1}: gap {gap:.1f} pt, wanted {GAP}" for index, gap in enumerate(found) if gap is not None and abs(gap - GAP) > TOLERANCE]
