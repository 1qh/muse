"""The chord size every check sizes against: the most common size of chord-shaped text, never the largest text."""
import pathlib

import pymupdf
import pytest

from muse import chordseat


def page_with(path: pathlib.Path, words: list[tuple[str, float]]) -> pathlib.Path:
    document = pymupdf.open()
    page = document.new_page()
    for at, (text, size) in enumerate(words):
        page.insert_text((40, 60 + at * 40), text, fontsize=size)
    document.save(path)
    return path


def test_chord_size_ignores_larger_text(tmp_path: pathlib.Path) -> None:
    pdf = page_with(tmp_path / "s.pdf", [("Title of the song", 22.9), ("B", 16.0), ("Em", 12.8), ("Bm7", 12.8), ("Am/C", 12.8), ("Tacet", 8.2)])
    assert chordseat.chord_size(pdf) == 12.8


def test_chord_size_refuses_a_page_without_chords(tmp_path: pathlib.Path) -> None:
    pdf = page_with(tmp_path / "s.pdf", [("Title of the song", 22.9), ("Tacet", 8.2)])
    with pytest.raises(ValueError, match="no chord text"):
        chordseat.chord_size(pdf)
