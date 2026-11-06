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


def test_collisions_judge_chords_beside_a_larger_mark(tmp_path: pathlib.Path) -> None:
    from muse import pdfcheck

    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((40, 40), "B", fontsize=16.0)
    page.insert_text((40, 120), "Em", fontsize=12.8)
    page.insert_text((57, 120), "Am", fontsize=12.8)  # 2 chords nearly touching
    page.insert_text((40, 200), "Bm7", fontsize=12.8)
    path = tmp_path / "c.pdf"
    document.save(path)
    assert [root for _, _, root in pdfcheck.chord_collisions(path)] == ["A"]
