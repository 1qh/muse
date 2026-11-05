"""Checks on a built sheet that need no MuseScore."""
import zipfile

import pymupdf

from muse import chordnames, pdfcheck, port


def blank_pdf(path, pages=2):
    doc = pymupdf.open()
    for _ in range(pages):
        doc.new_page()
    doc.save(path)
    return path


def test_a_fault_on_a_page_with_no_bar_number_lands_on_the_last_numbered_line(tmp_path, monkeypatch):
    pdf = blank_pdf(tmp_path / "p.pdf")
    monkeypatch.setattr(pdfcheck, "chord_collisions", lambda _: [(1, 100.0, "x")])
    monkeypatch.setattr(pdfcheck, "past_margin", lambda _: [])
    monkeypatch.setattr(pdfcheck, "text_collisions", lambda _: [])
    assert port.crowded_rows(pdf, [1]) == {1}


def test_a_text_fault_on_a_page_with_no_bar_number_lands_on_the_last_numbered_line(tmp_path, monkeypatch):
    pdf = blank_pdf(tmp_path / "p.pdf")
    monkeypatch.setattr(pdfcheck, "chord_collisions", lambda _: [])
    monkeypatch.setattr(pdfcheck, "past_margin", lambda _: [(1, 100.0, "x")])
    monkeypatch.setattr(pdfcheck, "text_collisions", lambda _: [])
    assert port.crowded_rows(pdf, [1]) == {1}


def test_the_floor_with_a_wide_line_gap_is_a_fault():
    assert port.floor_gap_problems("tone", {"spatium": 1.2, "distance": 22.0})
    assert not port.floor_gap_problems("tone", {"spatium": 1.2, "distance": 11.0})
    assert not port.floor_gap_problems("tone", {"spatium": 1.5, "distance": 22.0})


def test_the_build_path_leaves_the_style_and_the_chord_list_stays(tmp_path):
    score = tmp_path / "s.mscz"
    with zipfile.ZipFile(score, "w") as z:
        z.writestr("s.mss", "<chordDescriptionFile>/build/chords_short.xml</chordDescriptionFile>")
        z.writestr("chordlist.xml", "<chords/>")
    assert chordnames.chord_path_problems(score)
    chordnames.drop_chord_path(score)
    assert chordnames.chord_path_problems(score) == []
    with zipfile.ZipFile(score) as z:
        assert z.read("chordlist.xml") == b"<chords/>"
        assert b"/build" not in z.read("s.mss")
