"""Refuse a page number, header or footer in a finished PDF, read from word positions."""
from __future__ import annotations

import pathlib
import re
import sys

import pymupdf

CHORD = re.compile(r"(?:[A-G][#b♯♭]?)?(?:maj|min|dim|sus|add|aug|m|\d|[#b♯♭()/ ]|[A-G][#b♯♭]?)*")
MIN_CHORD_GAP = 0.75  # clear space between 2 chords, in chord font sizes
MAX_TEXT_BOX = 40.0  # taller word boxes are music font symbols, and no footer is this tall
RIGHT_LIMIT = 0.975  # the 0.3 inch right margin, with 2 points of tolerance
TOUCHING_HEIGHT = 0.5
JUMP_WORDS = ("D.S.", "al", "To", "Coda")
TEXT_OVERLAP = 2.0  # slanted words of 1 phrase overlap by less
JUMP_GAP = 4.0
SIGNS = "\ue047\ue048"
SIGN_OVER_CHORD = 1.6
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


def all_problems(path: pathlib.Path, title: str) -> list[str]:
    return text_problems(path) + title_problems(path, title) + sign_problems(path) + [f"text on text: {c[2]}" for c in text_collisions(path)]


def main() -> None:
    bad = 0
    for path in sorted(pathlib.Path(sys.argv[1]).glob("*.pdf")):
        found = text_problems(path)
        print(path.name, "margin words", found)
        bad += len(found)
    raise SystemExit(1 if bad else 0)


if __name__ == "__main__":
    main()
