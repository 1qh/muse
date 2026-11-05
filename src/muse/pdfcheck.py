"""Refuse a page number, header or footer in a finished PDF, read from word positions."""
from __future__ import annotations

import pathlib
import re
import sys

import pymupdf

from muse import chordseat

CHORD = re.compile(r"(?:[A-G][#b♯♭]?)?(?:maj|min|dim|sus|add|aug|m|\d|[#b♯♭()/ Δø°]|[A-G][#b♯♭]?)*")
MIN_CHORD_GAP = 0.75  # clear space between 2 chords, in chord font sizes
MAX_TEXT_BOX = 40.0  # taller word boxes are music font symbols, and no footer is this tall
RIGHT_LIMIT = 0.975  # the 0.3 inch right margin, with 2 points of tolerance
TOUCHING_HEIGHT = 0.5
JUMP_WORDS = ("D.S.", "al", "To", "Coda")
TEXT_OVERLAP = 2.0  # slanted words of 1 phrase overlap by less
JUMP_GAP = 4.0
SIGNS = "\ue047\ue048"
CHORD_FLOOR = 9.0  # points: text this large starting with a note letter is a chord
COVER_SLACK = 1.5  # points a sign may graze a chord letter
ROW_SLACK = 10.0  # points a sign baseline may rise above its line's chord baseline
SIGN_OVER_CHORD = 1.6
SIGN_TOUCH_SLACK = 1.0
MAX_CHORD_SPACES = 2.5  # chord baseline above the staff top, on a line with no notes
SIGN_INK_WIDTH = 0.75  # the printed part of a sign, in sign font sizes
SIGN_BASELINE_SHARE = 0.3  # a sign higher than this share of the chord size clears the letters
CLEFS = {0xE050, 0xE052, 0xE053}
HEAD_CODES = (0xE0A0, 0xE0FF)
MIN_CLEF_SIZE = 8.0
BAR_NUMBER_X = 70.0
STAFF_REACH = 40.0
MUSIC_FONTS = ("Leland", "Bravura", "MScore", "mscore")
TOP_BAND = 0.015  # the style keeps text out of the page top, music above the first staff included
BOTTOM_BAND = 0.992  # no word reaches the bottom edge region: printable music ends above it


def margin_words(path: pathlib.Path) -> list[str]:
    found = []
    for number, page in enumerate(pymupdf.open(path), 1):
        height = page.rect.height
        for word in page.get_text("words"):
            y0, y1, text = word[1], word[3], word[4]
            if y1 - y0 > MAX_TEXT_BOX or all(0xE000 <= ord(c) <= 0xF8FF for c in text):
                continue  # music font symbols report oversized boxes; only text can be a header or footer
            if y0 < height * TOP_BAND or y1 > height * BOTTOM_BAND or (text.isdigit() and text == str(number) and y0 > height * 0.9):
                found.append(f"p{number}:{text}")
    return found


def foreign_fonts(path: pathlib.Path) -> list[str]:
    """Any text font but the style's own means a letter fell back mid-word."""
    names = {font[3].split("+")[-1] for page in pymupdf.open(path) for font in page.get_fonts()}
    return sorted(name for name in names if not name.startswith(MUSIC_FONTS + ("SFPro", "SF-Pro", "SF Pro")))


def oversized_text(path: pathlib.Path) -> list[str]:
    """Words at or above the biggest chord size; chords must be the biggest text on the page."""
    spans = []
    for page in pymupdf.open(path):
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    text = span["text"].strip()
                    if text and not all(0xE000 <= ord(c) <= 0xF8FF for c in text):
                        spans.append((round(span["size"], 1), text))
    top = max(size for size, _ in spans)
    return sorted({text for size, text in spans if size >= top and not CHORD.fullmatch(text)})


def chord_collisions(path: pathlib.Path) -> list[tuple[int, float, str]]:
    """Chord roots with too little clear space before them, read per character: page, height, root."""
    found = []
    for number, page in enumerate(pymupdf.open(path), 1):
        chars = []
        for block in page.get_text("rawdict")["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    for ch in span["chars"]:
                        if not ch["c"].isspace() and not 0xE000 <= ord(ch["c"]) <= 0xF8FF:
                            chars.append((span["size"], ch["origin"][1], ch["bbox"][0], ch["bbox"][2], ch["c"]))
        if not chars:
            continue
        top = max(c[0] for c in chars)
        ordered = sorted((c for c in chars if abs(c[0] - top) < 0.05), key=lambda c: (round(c[1], 0), c[2]))
        for before, after in zip(ordered, ordered[1:], strict=False):
            is_root = after[4] in "ABCDEFG" and before[4] != "/"
            if abs(before[1] - after[1]) < 1 and is_root and after[2] - before[3] < top * MIN_CHORD_GAP:
                found.append((number - 1, after[1] - top, after[4]))
    return found


def title_problems(path: pathlib.Path, title: str) -> list[str]:
    """The title prints exactly once in the top right corner of every page; lyrics may repeat its words."""
    problems = []
    spellings = {title, title.replace("Đ", "Ð")}  # SF Pro's text layer reports Đ as its look-alike
    for number, page in enumerate(pymupdf.open(path), 1):
        hits = [hit for spelling in spellings for hit in page.search_for(spelling)
                if hit.x1 > page.rect.width * 0.7 and hit.y1 < page.rect.height * 0.06]
        if len(hits) != 1:
            problems.append(f"p{number}: title printed {len(hits)} times at the top right")
    return problems


def past_margin(path: pathlib.Path) -> list[tuple[int, float, str]]:
    """Text running past the right margin: page, height, word."""
    found = []
    for number, page in enumerate(pymupdf.open(path), 1):
        for word in page.get_text("words"):
            if word[2] > page.rect.width * RIGHT_LIMIT and word[3] - word[1] <= MAX_TEXT_BOX:
                found.append((number - 1, word[1], word[4]))
    return found


def text_problems(path: pathlib.Path) -> list[str]:
    return margin_words(path) + foreign_fonts(path) + oversized_text(path) + [c[2] for c in chord_collisions(path)] + [w[2] for w in past_margin(path)]


def sign_sizes(path: pathlib.Path) -> tuple[list[float], float]:
    """Sizes of every printed segno and coda, and of the biggest chord text."""
    signs = []
    words = []
    for page in pymupdf.open(path):
        for block in page.get_text("rawdict")["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    text = "".join(c["c"] for c in span["chars"])
                    if any(c in text for c in SIGNS):
                        signs.append(span["size"])
                    elif text.strip() and not all(0xE000 <= ord(c) <= 0xF8FF for c in text.strip()):
                        words.append(span["size"])
    return signs, max(words, default=0.0)


def sign_row_problems(path: pathlib.Path) -> list[str]:
    """A jump sign sits in the chord row of the line it belongs to: no higher than that line's chord baseline allows."""
    problems = []
    for number, page in enumerate(pymupdf.open(path), 1):
        ys = sorted({round(d["rect"].y0, 1) for d in page.get_drawings() if d["rect"].width > 100 and d["rect"].height < 1})
        staves = [(ys[i], ys[i + 4]) for i in range(0, len(ys) - 4) if all(abs((ys[i + k + 1] - ys[i + k]) - (ys[i + 1] - ys[i])) < 0.5 for k in range(4))]
        staves = [s for k, s in enumerate(staves) if not k or s[0] > staves[k - 1][1]]
        signs, chords = [], []
        for block in page.get_text("rawdict")["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    text = "".join(c["c"] for c in span["chars"]).strip()
                    if any(c in SIGNS for c in text):
                        signs += [c["bbox"][3] for c in span["chars"] if c["c"] in SIGNS]
                    elif text and text[0] in "ABCDEFG" and span["size"] >= CHORD_FLOOR:
                        chords.append(span["origin"][1])
        for y in signs:
            below = [s for s in staves if s[0] >= y]
            if not below:
                continue
            above = [s for s in staves if s[1] < below[0][0]]
            floor = above[-1][1] if above else 0.0
            row = [c for c in chords if floor < c < below[0][0]]
            if y < min(row, default=below[0][0]) - ROW_SLACK:
                problems.append(f"page {number}: a jump sign at height {y:.0f} sits above the chord row of its line")
    return problems


def sign_problems(path: pathlib.Path) -> list[str]:
    """Signs must catch the eye: well above the chords. Jump words must be bold and below the chords."""
    signs, chords = sign_sizes(path)
    problems = [f"sign of {size:.1f} under {SIGN_OVER_CHORD} times chords of {chords:.1f}" for size in signs if size < chords * SIGN_OVER_CHORD]
    for page in pymupdf.open(path):
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    if span["text"].strip() in ("To Coda", "D.S. al Coda") and "bold" not in span["font"].lower():
                        problems.append(f"jump words not bold: {span['text'].strip()}")
    return problems


def sign_on_chord(path: pathlib.Path) -> list[str]:
    """A segno or coda on the baseline of the chord beside it, its ink running into the chord's first letter."""
    found = []
    for number, page in enumerate(pymupdf.open(path), 1):
        signs, chars, sign_boxes = [], [], []
        for block in page.get_text("rawdict")["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    for ch in span["chars"]:
                        if ch["c"] in SIGNS:
                            signs.append((ch["origin"], span["size"]))
                            sign_boxes.append(ch["bbox"])
                        elif not ch["c"].isspace() and not 0xE000 <= ord(ch["c"]) <= 0xF8FF:
                            chars.append((ch, span["size"]))
        top = max((size for _, size in chars), default=0.0)
        boxes = [(ch["bbox"][0], ch["bbox"][2], ch["origin"][1]) for ch, size in chars if abs(size - top) < 0.05]
        found += [f"p{number}: sign touches a chord" for (x, y), size in signs if sign_touches(x, y, size, top, boxes)]
        letters = [ch["bbox"] for ch, size in chars if abs(size - top) < 0.05]
        found += [f"p{number}: sign covers a chord" for box in sign_boxes if any(overlap(box, letter) > COVER_SLACK for letter in letters)]
    return found


def overlap(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    """Points 2 boxes share in their narrower direction; 0 when apart."""
    return max(0.0, min(min(a[2], b[2]) - max(a[0], b[0]), min(a[3], b[3]) - max(a[1], b[1])))


def sign_touches(x: float, y: float, size: float, chord_size: float, chord_letters: list[tuple[float, float, float]]) -> bool:
    """Whether a sign's ink runs into a chord letter, given each letter's left, right and baseline."""
    ink_end = x + size * SIGN_INK_WIDTH - SIGN_TOUCH_SLACK  # a chord seated right after a sign meets it without touching
    return any(abs(base - y) < chord_size * SIGN_BASELINE_SHARE and x < right and left < ink_end for left, right, base in chord_letters)


def chord_seating(path: pathlib.Path) -> list[tuple[int, float, float, str]]:
    """Each chord's overhang left of a barline it crosses, and its height above the staff in staff spaces where the line has no notes: page, overhang, spaces, chord."""
    found = []
    for number, page in enumerate(pymupdf.open(path), 1):
        spans, heads = [], []
        for block in page.get_text("rawdict")["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    text = "".join(c["c"] for c in span["chars"]).strip()
                    if text and not all(0xE000 <= ord(c) <= 0xF8FF for c in text):
                        spans.append((span["size"], span["bbox"][0], span["bbox"][2], span["origin"][1], text))
                    heads += [c["origin"] for c in span["chars"] if HEAD_CODES[0] <= ord(c["c"]) <= HEAD_CODES[1]]
        if not spans:
            continue
        top = max(item[0] for item in spans)
        drawings = chordseat.segments(page)
        staves = chordseat.staves_of(drawings)
        for size, left, right, base, text in spans:
            if abs(size - top) >= 0.05 or not staves:
                continue
            below = [staff for staff in staves if staff[0] >= base - top]
            if not below:
                continue
            staff = below[0]
            gap = (staff[4] - staff[0]) / 4
            bars = [r.x0 for r in drawings if r.width < 3 and abs(r.y0 - staff[0]) < 1 and abs(r.y1 - staff[4]) < 1]
            overhang = max((bar - left for bar in bars if left < bar < right), default=0.0)
            noted = any(staff[0] - STAFF_REACH <= y <= staff[4] + STAFF_REACH for _, y in heads)
            found.append((number - 1, overhang, -1.0 if noted else (staff[0] - base) / gap, text))
    return found


def seating_problems(path: pathlib.Path) -> list[str]:
    """On a line with no notes a chord sits close above the staff; `chordseat.problems` judges its barline."""
    return [f"p{page + 1}: chord {text} {spaces:.1f} spaces above the staff" for page, _, spaces, text in chord_seating(path) if spaces > MAX_CHORD_SPACES]


def text_collisions(path: pathlib.Path) -> list[tuple[int, float, str]]:
    """Words printed on top of each other, or touching with no gap: page, height, the later word."""
    found = []
    for number, page in enumerate(pymupdf.open(path), 1):
        words = [w for w in page.get_text("words") if w[3] - w[1] <= MAX_TEXT_BOX and not all(0xE000 <= ord(c) <= 0xF8FF for c in w[4])]
        for i, a in enumerate(words):
            for b in words[i + 1:]:
                wide = min(a[2], b[2]) - max(a[0], b[0])
                tall = min(a[3], b[3]) - max(a[1], b[1])
                jump = (a[4] in JUMP_WORDS) != (b[4] in JUMP_WORDS)
                if wide > (-JUMP_GAP if jump else TEXT_OVERLAP) and tall > min(a[3] - a[1], b[3] - b[1]) * TOUCHING_HEIGHT:
                    found.append((number - 1, b[1], b[4]))
    return found


def empty_staves(path: pathlib.Path) -> list[str]:
    """A staff with no notehead sharing its line with another staff; a bar number starts each line after the first."""
    found = []
    for number, page in enumerate(pymupdf.open(path), 1):
        clefs: list[float] = []
        heads: list[float] = []
        starts: list[float] = []
        for block in page.get_text("rawdict")["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    for ch in span["chars"]:
                        code = ord(ch["c"])
                        if code in CLEFS and span["size"] > MIN_CLEF_SIZE:
                            clefs.append(ch["origin"][1])
                        elif HEAD_CODES[0] <= code <= HEAD_CODES[1]:
                            heads.append(ch["origin"][1])
                    text = "".join(ch["c"] for ch in span["chars"]).strip()
                    if text.isdigit() and span["bbox"][0] < BAR_NUMBER_X and "Italic" in span["font"]:
                        starts.append(span["bbox"][1])
        found += [f"p{number}: empty staff at height {clef:.0f}" for clef in idle_staves(clefs, heads, starts)]
    return found


def idle_staves(clefs: list[float], heads: list[float], starts: list[float]) -> list[float]:
    """Staff heights with no notehead on a line of several staves; duplicates of 1 clef height count once."""
    lines: dict[int, list[float]] = {}
    for clef in sorted(set(round(c, 1) for c in clefs)):
        lines.setdefault(sum(1 for start in starts if start < clef), []).append(clef)
    idle = []
    for staves in lines.values():
        if len(staves) < 2:
            continue
        for at, clef in enumerate(staves):
            high = (clef + staves[at - 1]) / 2 if at else clef - STAFF_REACH
            low = (clef + staves[at + 1]) / 2 if at + 1 < len(staves) else clef + STAFF_REACH
            if not any(high <= head <= low for head in heads):
                idle.append(clef)
    return idle


def all_problems(path: pathlib.Path, title: str) -> list[str]:
    return empty_staves(path) + text_problems(path) + title_problems(path, title) + sign_problems(path) + sign_row_problems(path) + sign_on_chord(path) + seating_problems(path) + [f"text on text: {c[2]}" for c in text_collisions(path)]


def main() -> None:
    bad = 0
    for path in sorted(pathlib.Path(sys.argv[1]).glob("*.pdf")):
        found = text_problems(path)
        print(path.name, "margin words", found)
        bad += len(found)
    raise SystemExit(1 if bad else 0)


if __name__ == "__main__":
    main()


BASS_CHORD = re.compile(r"^[A-G][^/]*/[A-G][\u266f\u266d]?$")
BASS_ALONE = re.compile(r"^/[A-G][\u266f\u266d]?$")


def bass_problems(pdf: pathlib.Path) -> list[str]:
    """Slash chords whose bass does not share the root's baseline and size."""
    problems = []
    for number, page in enumerate(pymupdf.open(pdf), 1):
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
