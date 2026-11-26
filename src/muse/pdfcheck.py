"""Refuse a page number, header or footer in a finished PDF, read from word positions."""

from __future__ import annotations

import itertools
import os
import pathlib
import re
import sys

import pymupdf

from muse import chordseat, pdftext
from muse.smufl import HEAD_CODES, PRIVATE_USE

CHORD = re.compile(r"(?:[A-G][#b♯♭]?)?(?:maj|min|dim|sus|add|aug|m|\d|[#b♯♭()/ Δø°]|[A-G][#b♯♭]?)*")
MIN_CHORD_GAP = chordseat.CHORD_GAP
MAX_TEXT_BOX = 40.0
RIGHT_LIMIT = 0.975
TOUCHING_HEIGHT = 0.5
JUMP_WORDS = ("D.S.", "al", "To", "Coda")
TEXT_OVERLAP = 2.0
JUMP_GAP = 4.0
SIGNS = "\ue047\ue048"
CHORD_FLOOR = 9.0
COVER_SLACK = 1.5
ROW_SLACK = 10.0
SIGN_OVER_CHORD = 1.6
MAX_CHORD_SPACES = 2.5
SIGN_BASELINE_SHARE = 0.3
CLEFS = {0xE050, 0xE052, 0xE053}
MIN_CLEF_SIZE = 8.0
BAR_NUMBER_X = 70.0
STAFF_REACH = 40.0
MUSIC_FONTS = ("Leland", "Bravura", "MScore", "mscore")
TOP_BAND = 0.015
BOTTOM_BAND = 0.992
CHORD_SIZE_TOLERANCE = 0.05
STAFF_LINE_MIN_WIDTH = 100
STAFF_SPACING_TOLERANCE = 0.5
MIN_STAVES_PER_LINE = 2


def _raw_spans(page: pdftext.Page) -> list[pdftext.Span]:
    """Return every span of a page's raw text, in reading order.

    Returns:
        Each span of every line of every block.

    """
    return [
        span for block in page.get_text("rawdict")["blocks"] for line in block.get("lines", []) for span in line["spans"]
    ]


def margin_words(path: pathlib.Path) -> list[str]:
    found = []
    for number, page in enumerate(pdftext.read(path), 1):
        height = page.rect.height
        italic = {
            (round(span["bbox"][0]), round(span["bbox"][1]))
            for block in page.get_text("dict")["blocks"]
            for line in block.get("lines", [])
            for span in line["spans"]
            if "Italic" in span["font"]
        }
        for word in page.get_text("words"):
            y0, y1, text = word[1], word[3], word[4]
            if y1 - y0 > MAX_TEXT_BOX or all(ord(c) in PRIVATE_USE for c in text):
                continue
            if text.isdigit() and (round(word[0]), round(y0)) in italic:
                continue
            if (
                y0 < height * TOP_BAND
                or y1 > height * BOTTOM_BAND
                or (text.isdigit() and text == str(number) and y0 > height * 0.9)
            ):
                found.append(f"p{number}:{text}")
    return found


def foreign_fonts(path: pathlib.Path) -> list[str]:
    """Any text font but the style's own means a letter fell back mid-word.

    Returns:
        The sorted names of every text font outside the style's own.

    """
    names = {font[3].split("+")[-1] for page in pdftext.read(path) for font in page.get_fonts()}
    return sorted(name for name in names if not name.startswith((*MUSIC_FONTS, "SFPro", "SF-Pro", "SF Pro")))


def oversized_text(path: pathlib.Path) -> list[str]:
    """Words at or above the biggest chord size; chords must be the biggest text on the page.

    Returns:
        The sorted words, other than chords, printed at the chord size or above.

    """
    spans = []
    for page in pdftext.read(path):
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    text = span["text"].strip()
                    if text and not all(ord(c) in PRIVATE_USE for c in text):
                        spans.append((round(span["size"], 1), text))
    top = chordseat.chord_size(path)
    return sorted({text for size, text in spans if size >= top - 0.05 and not CHORD.fullmatch(text)})


def chord_collisions(path: pathlib.Path) -> list[tuple[int, float, str]]:
    """Chord roots with too little clear space before them, read per character: page, height, root.

    Returns:
        Each crowded root as page, height and root letter.

    """
    found: list[tuple[int, float, str]] = []
    for number, page in enumerate(pdftext.read(path), 1):
        chars = []
        for block in page.get_text("rawdict")["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    chars.extend(
                        (span["size"], ch["origin"][1], ch["bbox"][0], ch["bbox"][2], ch["c"])
                        for ch in span["chars"]
                        if not ch["c"].isspace() and ord(ch["c"]) not in PRIVATE_USE
                    )
        if not chars:
            continue
        top = chordseat.chord_size(path)
        ordered = sorted(
            (c for c in chars if abs(c[0] - top) < CHORD_SIZE_TOLERANCE),
            key=lambda c: (round(c[1], 0), c[2]),
        )
        for before, after in itertools.pairwise(ordered):
            is_root = after[4] in "ABCDEFG" and before[4] != "/"
            if abs(before[1] - after[1]) < 1 and is_root and after[2] - before[3] < top * MIN_CHORD_GAP:
                found.append((number - 1, after[1] - top, after[4]))
                if os.environ.get("MUSE_LAYOUT_TRACE", ""):
                    sys.stdout.write(
                        f"collision p{number} y{after[1]:.0f}: '{before[4]}' then '{after[4]}' "
                        f"gap {after[2] - before[3]:.2f} of {top * MIN_CHORD_GAP:.2f} pt\n",
                    )
    return found


def title_problems(path: pathlib.Path, title: str) -> list[str]:
    """Check the title prints exactly once in the top right corner of every page; lyrics may repeat its words.

    Returns:
        1 problem per page whose top right corner holds the title other than once.

    """
    problems = []
    spellings = {title, title.replace("Đ", "Ð")}
    for number, page in enumerate(pdftext.read(path), 1):
        hits = [
            hit
            for spelling in spellings
            for hit in page.search_for(spelling)
            if hit.x1 > page.rect.width * 0.7 and hit.y1 < page.rect.height * 0.06
        ]
        if len(hits) != 1:
            problems.append(f"p{number}: title printed {len(hits)} times at the top right")
    return problems


def off_page(path: pathlib.Path) -> list[tuple[int, float, str]]:
    """Text or a music glyph reaching past any edge of the page: page, height, what it is.

    Returns:
        Each span past an edge as page, height and description.

    """
    found: list[tuple[int, float, str]] = []
    for number, page in enumerate(pdftext.read(path), 1):
        box = page.rect
        for block in page.get_text("dict", flags=pymupdf.TEXT_ACCURATE_BBOXES)["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    x0, y0, x1, y1 = span["bbox"]
                    if span["text"].strip() and (x0 < box.x0 or y0 < box.y0 or x1 > box.x1 or y1 > box.y1):
                        found.append((number - 1, y0, f"off the page: {span['text'].strip()[:20]}"))
    return found


def past_margin(path: pathlib.Path) -> list[tuple[int, float, str]]:
    """Text running past the right margin: page, height, word.

    Returns:
        Each word past the margin as page, height and word.

    """
    found: list[tuple[int, float, str]] = []
    for number, page in enumerate(pdftext.read(path), 1):
        found.extend(
            (number - 1, word[1], word[4])
            for word in page.get_text("words")
            if word[2] > page.rect.width * RIGHT_LIMIT and word[3] - word[1] <= MAX_TEXT_BOX
        )
    return found


def text_problems(path: pathlib.Path) -> list[str]:
    return (
        margin_words(path)
        + foreign_fonts(path)
        + oversized_text(path)
        + [c[2] for c in chord_collisions(path)]
        + [w[2] for w in past_margin(path)]
        + [w[2] for w in off_page(path)]
    )


def sign_sizes(path: pathlib.Path) -> tuple[list[float], float]:
    """Sizes of every printed segno and coda, and the size chords print at.

    Returns:
        The sign sizes and the chord size.

    """
    signs: list[float] = []
    for page in pdftext.read(path):
        for block in page.get_text("rawdict")["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    text = "".join(c["c"] for c in span["chars"])
                    if any(c in text for c in SIGNS):
                        signs.append(span["size"])
    return signs, chordseat.chord_size(path)


def sign_row_problems(path: pathlib.Path) -> list[str]:
    """Find jump signs set above the chord row of the line they belong to.

    A jump sign sits in the chord row of the line it belongs to: no higher than that line's chord baseline allows.

    Returns:
        1 problem per jump sign above its chord row.

    """
    problems = []
    for number, page in enumerate(pdftext.read(path), 1):
        ys = sorted({
            round(d["rect"].y0, 1)
            for d in page.get_drawings()
            if d["rect"].width > STAFF_LINE_MIN_WIDTH and d["rect"].height < 1
        })
        staves = [
            (ys[i], ys[i + 4])
            for i in range(len(ys) - 4)
            if all(abs((ys[i + k + 1] - ys[i + k]) - (ys[i + 1] - ys[i])) < STAFF_SPACING_TOLERANCE for k in range(4))
        ]
        staves = [s for k, s in enumerate(staves) if not k or s[0] > staves[k - 1][1]]
        signs, chords = [], []
        for block in page.get_text("rawdict")["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    text = "".join(c["c"] for c in span["chars"]).strip()
                    if any(c in SIGNS for c in text):
                        signs += [c["bbox"][3] for c in span["chars"] if c["c"] in SIGNS]
                    elif chordseat.CHORD_SHAPE.match(text) and span["size"] >= CHORD_FLOOR:
                        chords.append(span["origin"][1])
        for y in signs:
            below = [s for s in staves if s[1] >= y]
            if not below:
                continue
            above = [s for s in staves if s[1] < below[0][0]]
            floor = above[-1][1] if above else 0.0
            row = [c for c in chords if floor < c < below[0][0]]
            if y < min(row, default=below[0][0]) - ROW_SLACK:
                problems.append(f"page {number}: a jump sign at height {y:.0f} sits above the chord row of its line")
    return problems


def sign_problems(path: pathlib.Path) -> list[str]:
    """Signs must catch the eye: well above the chords. Jump words must be bold and below the chords.

    Returns:
        1 problem per sign too small and per jump phrase not bold.

    """
    signs, chords = sign_sizes(path)
    problems = [
        f"sign of {size:.1f} under {SIGN_OVER_CHORD} times chords of {chords:.1f}"
        for size in signs
        if size < chords * SIGN_OVER_CHORD
    ]
    for page in pdftext.read(path):
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                problems.extend(
                    f"jump words not bold: {span['text'].strip()}"
                    for span in line["spans"]
                    if span["text"].strip() in {"To Coda", "D.S. al Coda"} and "bold" not in span["font"].lower()
                )
    return problems


def sign_on_chord(path: pathlib.Path) -> list[str]:
    """Name each segno or coda on the baseline of the chord beside it, its ink running into the chord's first letter.

    Returns:
        1 problem per sign touching or covering a chord, named by page.

    """
    return [f"p{page + 1}: {what}" for page, _, what in sign_chord_hits(path)]


def sign_chord_hits(path: pathlib.Path) -> list[tuple[int, float, str]]:
    """Each sign touching or covering a chord: page, height, what; the height lets a layout find the line.

    Returns:
        Each hit as page, height and what the sign does to the chord.

    """
    found: list[tuple[int, float, str]] = []
    for number, page in enumerate(pdftext.read(path), 1):
        signs, chars, sign_boxes = [], [], []
        for span in _raw_spans(page):
            for ch in span["chars"]:
                if ch["c"] in SIGNS:
                    signs.append((ch["origin"], span["size"]))
                    sign_boxes.append(ch["bbox"])
                elif not ch["c"].isspace() and ord(ch["c"]) not in PRIVATE_USE:
                    chars.append((ch, span["size"]))
        top = chordseat.chord_size(path)
        boxes = [
            (ch["bbox"][0], ch["bbox"][2], ch["origin"][1]) for ch, size in chars if abs(size - top) < CHORD_SIZE_TOLERANCE
        ]
        found += [
            (number - 1, y, "sign touches a chord") for (x, y), size in signs if sign_touches(x, y, size, top, boxes)
        ]
        letters = [ch["bbox"] for ch, size in chars if abs(size - top) < CHORD_SIZE_TOLERANCE]
        found += [
            (number - 1, box[3], "sign covers a chord")
            for box in sign_boxes
            if any(overlap(box, letter) > COVER_SLACK for letter in letters)
        ]
    return found


def overlap(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    """Points 2 boxes share in their narrower direction; 0 when apart.

    Returns:
        The shared distance in the narrower direction.

    """
    return max(0.0, min(min(a[2], b[2]) - max(a[0], b[0]), min(a[3], b[3]) - max(a[1], b[1])))


def sign_touches(
    x: float,
    y: float,
    size: float,
    chord_size: float,
    chord_letters: list[tuple[float, float, float]],
) -> bool:
    """Whether a sign's ink runs into a chord letter, given each letter's left, right and baseline.

    Returns:
        Whether the sign's ink runs into any chord letter.

    """
    ink_end = chordseat.sign_ink_end(x, size)
    return any(
        abs(base - y) < chord_size * SIGN_BASELINE_SHARE and x < right and left < ink_end
        for left, right, base in chord_letters
    )


def chord_seating(path: pathlib.Path) -> list[tuple[int, float, float, str]]:
    """Each chord's overhang left of a barline it crosses, and its height above the staff.

    The height is in staff spaces where the line has no notes: page, overhang, spaces, chord.

    Returns:
        Each chord as page, overhang, spaces above the staff (-1.0 on a line with notes) and chord text.

    """
    found: list[tuple[int, float, float, str]] = []
    for number, page in enumerate(pdftext.read(path), 1):
        spans, heads = [], []
        tops: list[tuple[float, float]] = []
        for block in page.get_text("rawdict")["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    text = "".join(c["c"] for c in span["chars"]).strip()
                    if text and not all(ord(c) in PRIVATE_USE for c in text):
                        spans.append((span["size"], span["bbox"][0], span["bbox"][2], span["origin"][1], text))
                        tops.append((span["size"], span["bbox"][1]))
                    heads += [c["origin"] for c in span["chars"] if HEAD_CODES[0] <= ord(c["c"]) <= HEAD_CODES[1]]
        if not spans:
            continue
        top = chordseat.chord_size(path)
        drawings = chordseat.segments(page)
        staves = chordseat.staves_of(drawings)
        for size, left, right, base, text in spans:
            if abs(size - top) >= CHORD_SIZE_TOLERANCE or not staves:
                continue
            below = [staff for staff in staves if staff[0] >= base - top]
            if not below:
                continue
            staff = below[0]
            gap = (staff[4] - staff[0]) / 4
            bars = [
                r.x0
                for r in drawings
                if r.width < chordseat.BARLINE_MAX_WIDTH and abs(r.y0 - staff[0]) < 1 and abs(r.y1 - staff[4]) < 1
            ]
            overhang = max((bar - left for bar in bars if left < bar < right), default=0.0)
            noted = any(staff[0] - STAFF_REACH <= y <= staff[4] + STAFF_REACH for _, y in heads)
            floor = min(
                [
                    *(r.y0 for r in drawings if base < r.y0 < staff[0] - 0.5),
                    *(y for other, y in tops if abs(other - top) >= CHORD_SIZE_TOLERANCE and base < y < staff[0] - 0.5),
                ],
                default=staff[0],
            )
            found.append((number - 1, overhang, -1.0 if noted else (floor - base) / gap, text))
    return found


def seating_problems(path: pathlib.Path) -> list[str]:
    """On a line with no notes a chord sits close above the staff; `chordseat.problems` judges its barline.

    Returns:
        1 problem per chord seated too far above its staff.

    """
    return [
        f"p{page + 1}: chord {text} {spaces:.1f} spaces above the staff"
        for page, _, spaces, text in chord_seating(path)
        if spaces > MAX_CHORD_SPACES
    ]


def text_collisions(path: pathlib.Path) -> list[tuple[int, float, str]]:
    """Words printed on top of each other, or touching with no gap: page, height, the later word.

    Returns:
        Each collision as page, height and the later word.

    """
    found: list[tuple[int, float, str]] = []
    for number, page in enumerate(pdftext.read(path), 1):
        words = [
            w
            for w in page.get_text("words")
            if w[3] - w[1] <= MAX_TEXT_BOX and not all(ord(c) in PRIVATE_USE for c in w[4])
        ]
        for i, a in enumerate(words):
            for b in words[i + 1 :]:
                wide = min(a[2], b[2]) - max(a[0], b[0])
                tall = min(a[3], b[3]) - max(a[1], b[1])
                jump = (a[4] in JUMP_WORDS) != (b[4] in JUMP_WORDS)
                if wide > (-JUMP_GAP if jump else TEXT_OVERLAP) and tall > min(a[3] - a[1], b[3] - b[1]) * TOUCHING_HEIGHT:
                    found.append((number - 1, b[1], b[4]))
    return found


def empty_staves(path: pathlib.Path) -> list[str]:
    """Find a staff with no notehead sharing its line with another staff.

    A bar number starts each line after the first.

    Returns:
        1 problem per empty staff, named by page and height.

    """
    found: list[str] = []
    try:
        size: float | None = chordseat.chord_size(path)
    except ValueError:
        size = None
    for number, page in enumerate(pdftext.read(path), 1):
        clefs: list[float] = []
        heads: list[float] = []
        chords: list[float] = []
        starts: list[float] = []
        for span in _raw_spans(page):
            for ch in span["chars"]:
                code = ord(ch["c"])
                if code in CLEFS and span["size"] > MIN_CLEF_SIZE:
                    clefs.append(ch["origin"][1])
                elif HEAD_CODES[0] <= code <= HEAD_CODES[1]:
                    heads.append(ch["origin"][1])
            text = "".join(ch["c"] for ch in span["chars"]).strip()
            if text.isdigit() and span["bbox"][0] < BAR_NUMBER_X and "Italic" in span["font"]:
                starts.append(span["bbox"][1])
            elif size is not None and text and abs(span["size"] - size) < CHORD_SIZE_TOLERANCE and CHORD.fullmatch(text):
                chords.append(span["origin"][1])
        found += [f"p{number}: empty staff at height {clef:.0f}" for clef in idle_staves(clefs, heads, starts, chords)]
    return found


def idle_staves(
    clefs: list[float],
    heads: list[float],
    starts: list[float],
    chords: list[float] | None = None,
) -> list[float]:
    """Staff heights with no notehead and no chord row of their own above them, on a line of several staves.

    Duplicates of 1 clef height count once.

    Returns:
        The clef height of every idle staff.

    """
    lines: dict[int, list[float]] = {}
    for clef in sorted({round(c, 1) for c in clefs}):
        lines.setdefault(sum(1 for start in starts if start < clef), []).append(clef)
    idle = []
    for staves in lines.values():
        if len(staves) < MIN_STAVES_PER_LINE:
            continue
        for at, clef in enumerate(staves):
            high = (clef + staves[at - 1]) / 2 if at else clef - STAFF_REACH
            low = (clef + staves[at + 1]) / 2 if at + 1 < len(staves) else clef + STAFF_REACH
            if not any(high <= head <= low for head in heads) and not any(high <= chord <= clef for chord in chords or []):
                idle.append(clef)
    return idle


SIGN_WORD_OVERLAP = 0.5


DYNAMICS = (0xE520, 0xE54F)
TEMPO_NOTES = (0xE1D0, 0xE1EF)


def ink_words(page: pymupdf.Page | pdftext.Page) -> list[tuple[str, pymupdf.Rect]]:
    """Every span's letters with their ink box.

    Music glyphs are left out, as a sign's own glyph sits inside its box by right.

    Returns:
        Each span's letters and the union of their ink boxes.

    """
    found: list[tuple[str, pymupdf.Rect]] = []
    for block in page.get_text("rawdict", flags=pymupdf.TEXT_ACCURATE_BBOXES)["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                letters = [c for c in span["chars"] if c["c"].strip() and ord(c["c"]) not in PRIVATE_USE]
                if letters:
                    box = pymupdf.Rect(letters[0]["bbox"])
                    for c in letters[1:]:
                        box |= pymupdf.Rect(c["bbox"])
                    found.append(("".join(c["c"] for c in letters), box))
    return found


def symbol_boxes(page: pymupdf.Page | pdftext.Page) -> list[pymupdf.Rect]:
    """Collect the ink box of every dynamic and tempo-note glyph on a page.

    Returns:
        The ink box of each such glyph.

    """
    return [
        pymupdf.Rect(c["bbox"])
        for block in page.get_text("rawdict", flags=pymupdf.TEXT_ACCURATE_BBOXES)["blocks"]
        for line in block.get("lines", [])
        for span in line["spans"]
        for c in span["chars"]
        if any(lo <= ord(c["c"]) <= hi for lo, hi in (DYNAMICS, TEMPO_NOTES))
    ]


def words_over(number: int, words: list[tuple[str, pymupdf.Rect]], boxes: list[pymupdf.Rect], what: str) -> list[str]:
    """Each word whose ink crosses one of the boxes by more than a hair, named with the page and what it lies under.

    Returns:
        1 problem per word under a box.

    """
    found = []
    for word, box in words:
        for frame in boxes:
            common = frame & box
            if not common.is_empty and common.width > SIGN_WORD_OVERLAP and common.height > SIGN_WORD_OVERLAP:
                found.append(f"text on text: p{number}: {what} over {word!r}")
                break
    return found


def _is_colored_sign_frame(drawing: pdftext.Drawing) -> bool:
    color = drawing.get("color")
    width = float(drawing["rect"].width)
    return (
        color is not None
        and len(color) > 0
        and tuple(color) != (0.0, 0.0, 0.0)
        and chordseat.SIGN_FRAME[0] < width < chordseat.SIGN_FRAME[1]
    )


def sign_word_overlaps(path: pathlib.Path) -> list[float]:
    """Measure the width in points of every overlap between a sign's coloured box and a printed word's ink.

    Returns:
        The width of each overlap.

    """
    found: list[float] = []
    for page in pdftext.read(path):
        frames = [d["rect"] for d in page.get_drawings() if _is_colored_sign_frame(d)]
        for _, box in ink_words(page) if frames else []:
            for frame in frames:
                common = frame & box
                if not common.is_empty and common.width > SIGN_WORD_OVERLAP and common.height > SIGN_WORD_OVERLAP:
                    found.append(common.width)
    return found


def sign_text_overlaps(path: pathlib.Path) -> list[str]:
    """Find a sign's coloured box over any printed word, a bar number, chord or mark.

    The box hides what the reader navigates by.

    Returns:
        1 problem per word under a sign box.

    """
    found: list[str] = []
    for number, page in enumerate(pdftext.read(path), 1):
        frames = [d["rect"] for d in page.get_drawings() if _is_colored_sign_frame(d)]
        if frames:
            found += words_over(number, ink_words(page), frames, "sign box")
    return found


def symbol_text_overlaps(path: pathlib.Path) -> list[str]:
    """Find a dynamic or a tempo note under a printed word.

    The word check reads letters only and never sees them.

    Returns:
        1 problem per word over a dynamic or tempo note.

    """
    found: list[str] = []
    for number, page in enumerate(pdftext.read(path), 1):
        boxes = symbol_boxes(page)
        if boxes:
            found += words_over(number, ink_words(page), boxes, "dynamic or tempo note")
    return found


BLANK_BAND = 0.15


INK_LEVEL = 128


def grey_ink_profile(page: pymupdf.Page, dpi: int) -> list[int]:
    """Count the grey pixels below the ink threshold in each page row.

    Returns:
        The ink count for each row at the requested resolution.

    """
    pixmap = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
    pixels = pixmap.samples
    return [
        sum(pixel < INK_LEVEL for pixel in pixels[row * pixmap.width : row * pixmap.width + pixmap.width])
        for row in range(pixmap.height)
    ]


def render_page_clip(
    page: pymupdf.Page,
    zoom: float,
    clip: pymupdf.Rect | None = None,
    *,
    grey: bool = False,
) -> pymupdf.Pixmap:
    """Render a page clip at a uniform zoom in RGB or grey.

    Returns:
        The rendered pixels and their dimensions.

    """
    return page.get_pixmap(
        matrix=pymupdf.Matrix(zoom, zoom),
        clip=clip,
        colorspace=pymupdf.csGRAY if grey else pymupdf.csRGB,
    )


def identical_pixels(rendered: pathlib.Path, published: pathlib.Path, dpi: int) -> bool:
    """Compare the page counts, pixel dimensions and pixel values of two PDFs.

    Returns:
        Whether every page has identical pixels at the requested resolution.

    """
    with pymupdf.open(rendered) as actual, pymupdf.open(published) as expected:
        same = len(actual) == len(expected)
        if same:
            for actual_page, expected_page in zip(actual, expected, strict=True):
                left = actual_page.get_pixmap(dpi=dpi)
                right = expected_page.get_pixmap(dpi=dpi)
                same = same and (left.width, left.height, left.samples) == (right.width, right.height, right.samples)
        return same


def inked_rows(page: pymupdf.Page, dpi: int) -> tuple[list[int], int]:
    """Find the pixel rows holding any ink at this resolution, and the page's height in rows.

    Returns:
        The inked row indexes and the page height in rows.

    """
    pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY, alpha=False)
    return [
        y for y in range(pix.height) if min(pix.samples[y * pix.stride : y * pix.stride + pix.width]) < INK_LEVEL
    ], int(pix.height)


def inked_columns(page: pymupdf.Page, dpi: int) -> tuple[list[int], int]:
    """Find the pixel columns holding any ink at this resolution, and the page's width in columns.

    Returns:
        The inked column indexes and the page width in columns.

    """
    pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY, alpha=False)
    return [
        x for x in range(pix.width) if min(pix.samples[x + y * pix.stride] for y in range(pix.height)) < INK_LEVEL
    ], int(pix.width)


def blank_bands(path: pathlib.Path) -> list[str]:
    """Every page leaving a blank band taller than BLANK_BAND of its height between its first and last ink.

    Returns:
        1 problem per page with such a band.

    """
    found = []
    for number, page in enumerate(pdftext.read(path), 1):
        rows, height = inked_rows(page.page, 36)
        gap = max((b - a for a, b in itertools.pairwise(rows)), default=0) / height
        if gap > BLANK_BAND:
            found.append(f"p{number}: a blank band of {gap:.0%} of the page between its ink")
    return found


A4 = (595.0, 842.0)
A4_TOLERANCE = 1.5
EDGE = 8.0
LOW_DPI = 72


def paper_problems(path: pathlib.Path) -> list[str]:
    problems = []
    doc = pymupdf.open(path)
    for number, page in enumerate(doc.pages(), 1):
        if abs(page.rect.width - A4[0]) > A4_TOLERANCE or abs(page.rect.height - A4[1]) > A4_TOLERANCE:
            problems.append(f"p{number}: not A4 ({page.rect.width:.0f} x {page.rect.height:.0f})")
    return problems


def ink_problems(path: pathlib.Path) -> list[str]:
    problems = []
    for number, page in enumerate(pymupdf.open(path).pages(), 1):
        rows, height = inked_rows(page, LOW_DPI)
        cols, width = inked_columns(page, LOW_DPI)
        if not rows:
            problems.append(f"p{number}: blank page")
        elif rows[0] < EDGE or rows[-1] > height - EDGE or cols[0] < EDGE or cols[-1] > width - EDGE:
            problems.append(f"p{number}: ink within {EDGE:g} points of the edge")
    return problems


def all_problems(path: pathlib.Path, title: str) -> list[str]:
    page = paper_problems(path) + ink_problems(path) + blank_bands(path)
    return (
        page
        + empty_staves(path)
        + text_problems(path)
        + title_problems(path, title)
        + sign_problems(path)
        + sign_row_problems(path)
        + sign_on_chord(path)
        + seating_problems(path)
        + [f"text on text: {c[2]}" for c in text_collisions(path)]
        + sign_text_overlaps(path)
        + symbol_text_overlaps(path)
    )


def main() -> None:
    bad = 0
    for path in sorted(pathlib.Path(sys.argv[1]).glob("*.pdf")):
        found = text_problems(path)
        sys.stdout.write(f"{path.name} margin words {found}\n")
        bad += len(found)
    raise SystemExit(1 if bad else 0)


if __name__ == "__main__":
    main()


BASS_CHORD = re.compile(r"^[A-G][^/]*/[A-G][\u266f\u266d]?$")
BASS_ALONE = re.compile(r"^/[A-G][\u266f\u266d]?$")


def bass_problems(pdf: pathlib.Path) -> list[str]:
    """Slash chords whose bass does not share the root's baseline and size.

    Returns:
        1 problem per slash chord set apart or split across baselines or sizes.

    """
    problems = []
    for number, page in enumerate(pdftext.read(pdf), 1):
        for block in page.get_text("rawdict")["blocks"]:
            for line in block.get("lines", []):
                text = "".join(c["c"] for span in line["spans"] for c in span["chars"]).strip()
                if BASS_ALONE.match(text):
                    problems.append(f"page {number}: bass {text} set apart from its root")
                elif BASS_CHORD.match(text):
                    heights = {round(c["origin"][1], 1) for span in line["spans"] for c in span["chars"] if c["c"].strip()}
                    sizes = {round(span["size"], 1) for span in line["spans"]}
                    if len(heights) > 1 or len(sizes) > 1:
                        problems.append(f"page {number}: chord {text} baselines {sorted(heights)} sizes {sorted(sizes)}")
    return problems
