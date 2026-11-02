"""Refuse a page number, header or footer in a finished PDF, read from word positions."""
from __future__ import annotations

import pathlib
import sys

import pymupdf

BAND = 0.03  # the page margin: the style keeps all music inside it


def margin_words(path: pathlib.Path) -> list[str]:
    found = []
    for number, page in enumerate(pymupdf.open(path), 1):
        height = page.rect.height
        for word in page.get_text("words"):
            y0, y1, text = word[1], word[3], word[4]
            if y1 < height * BAND or y0 > height * (1 - BAND) or (text.isdigit() and text == str(number) and y0 > height * 0.9):
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
