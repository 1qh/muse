"""Refuse a page number, header or footer in a finished PDF, read from word positions."""
from __future__ import annotations

import pathlib
import sys

import pymupdf

TOP_BAND = 0.015  # the style keeps text out of the page top, music above the first staff included
BOTTOM_BAND = 0.992  # no word reaches the bottom edge region: printable music ends above it


def margin_words(path: pathlib.Path) -> list[str]:
    found = []
    for number, page in enumerate(pymupdf.open(path), 1):
        height = page.rect.height
        for word in page.get_text("words"):
            y0, y1, text = word[1], word[3], word[4]
            if all(0xE000 <= ord(c) <= 0xF8FF for c in text):
                continue  # music font symbols report oversized boxes; only text can be a header or footer
            if y0 < height * TOP_BAND or y1 > height * BOTTOM_BAND or (text.isdigit() and text == str(number) and y0 > height * 0.9):
                found.append(f"p{number}:{text}")
    return found


def main() -> None:
    bad = 0
    for path in sorted(pathlib.Path(sys.argv[1]).glob("*.pdf")):
        found = margin_words(path)
        print(path.name, "margin words", found)
        bad += len(found)
    raise SystemExit(1 if bad else 0)


if __name__ == "__main__":
    main()
