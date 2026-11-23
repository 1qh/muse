"""Checks on a built sheet that need no MuseScore."""

import pathlib
import zipfile

import pymupdf
import pytest

from muse import chordnames, pdfcheck, pdftext, port, scorefile


def blank_pdf(path: pathlib.Path, pages: int = 2) -> pathlib.Path:
    doc = pymupdf.open()
    for _ in range(pages):
        doc.new_page()
    doc.save(path)
    return path


def test_a_fault_on_a_page_with_no_bar_number_lands_on_the_last_numbered_line(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf = blank_pdf(tmp_path / "p.pdf")
    monkeypatch.setattr(pdfcheck, "chord_collisions", lambda _: [(1, 100.0, "x")])
    monkeypatch.setattr(pdfcheck, "sign_chord_hits", lambda _: [])
    monkeypatch.setattr(pdfcheck, "past_margin", lambda _: [])
    monkeypatch.setattr(pdfcheck, "text_collisions", lambda _: [])
    assert port.crowded_rows(pdf, [1]) == {1}


def test_text_on_text_is_never_a_width_fault(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdf = blank_pdf(tmp_path / "p.pdf")
    monkeypatch.setattr(pdfcheck, "chord_collisions", lambda _: [])
    monkeypatch.setattr(pdfcheck, "sign_chord_hits", lambda _: [])
    monkeypatch.setattr(pdfcheck, "past_margin", lambda _: [])
    monkeypatch.setattr(pdfcheck, "text_collisions", lambda _: [(0, 100.0, "x")])
    assert port.crowded_rows(pdf, [1]) == set()


def test_a_margin_fault_on_a_page_with_no_bar_number_lands_on_the_last_numbered_line(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf = blank_pdf(tmp_path / "p.pdf")
    monkeypatch.setattr(pdfcheck, "chord_collisions", lambda _: [])
    monkeypatch.setattr(pdfcheck, "sign_chord_hits", lambda _: [])
    monkeypatch.setattr(pdfcheck, "past_margin", lambda _: [(1, 100.0, "x")])
    monkeypatch.setattr(pdfcheck, "text_collisions", lambda _: [])
    assert port.crowded_rows(pdf, [1]) == {1}


def test_the_build_path_leaves_the_style_and_the_chord_list_stays(tmp_path: pathlib.Path) -> None:
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


def bass_pdf(path: pathlib.Path, *parts: tuple[float, float, float, str]) -> pathlib.Path:
    doc = pymupdf.open()
    page = doc.new_page()
    for x, y, size, text in parts:
        page.insert_text((x, y), text, fontsize=size)
    doc.save(path)
    return path


def test_a_bass_on_the_roots_baseline_and_size_passes(tmp_path: pathlib.Path) -> None:
    assert pdfcheck.bass_problems(bass_pdf(tmp_path / "b.pdf", (50, 100, 14, "A/C"))) == []


def test_a_bass_set_apart_from_its_root_is_refused(tmp_path: pathlib.Path) -> None:
    assert pdfcheck.bass_problems(bass_pdf(tmp_path / "b.pdf", (50, 100, 14, "A"), (70, 140, 10, "/C")))


def test_a_lyric_label_with_a_slash_is_no_bass(tmp_path: pathlib.Path) -> None:
    assert pdfcheck.bass_problems(bass_pdf(tmp_path / "b.pdf", (50, 100, 10, "/Perc"))) == []


def test_the_chord_file_keeps_the_bass_level() -> None:
    assert "<renderBase>/ :n :a</renderBase>" in chordnames.CHORD_FILE.read_text(encoding="utf-8")


def test_a_bass_a_little_lower_on_its_own_line_is_refused(tmp_path: pathlib.Path) -> None:
    found = pdfcheck.bass_problems(bass_pdf(tmp_path / "b.pdf", (50, 100, 14, "A"), (58, 101.5, 14, "/C")))
    assert found
    assert "baselines" in found[0]


def test_a_jump_without_its_box_or_with_words_is_refused(tmp_path: pathlib.Path) -> None:
    for body, found in (
        ("<Jump><text>D.S.</text></Jump>", "words"),
        ("<Jump><text><sym>segno</sym></text></Jump>", "no box"),
        (
            '<Jump><frameType>1</frameType><frameFgColor r="0" g="0" b="220" a="255"/>'
            "<text><sym>segno</sym></text></Jump>",
            "wrong box",
        ),
    ):
        path = tmp_path / "s.mscz"
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("s.mscx", f'<museScore version="4.70"><Score>{body}</Score></museScore>')
        assert any(found in problem for problem in scorefile.builtin_sign_problems(path))


def _row_pdf(path: pathlib.Path, sign_y: float) -> pathlib.Path:
    doc = pymupdf.open()
    page = doc.new_page()
    for top in (100.0, 200.0):
        for k in range(5):
            page.draw_line((40, top + 2 * k), (500, top + 2 * k))
    page.insert_text((60, 185), "Am7", fontsize=11)
    page.insert_text((450, sign_y), "S", fontsize=14)
    doc.save(path)
    return path


def test_a_jump_sign_above_its_chord_row_is_refused(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pdfcheck, "SIGNS", "S")
    assert pdfcheck.sign_row_problems(_row_pdf(tmp_path / "high.pdf", 150))
    assert pdfcheck.sign_row_problems(_row_pdf(tmp_path / "row.pdf", 186)) == []


def test_a_sign_over_a_chord_letter_is_refused() -> None:
    assert pdfcheck.overlap((0, 0, 10, 10), (8, 2, 14, 9)) == 2
    assert pdfcheck.overlap((0, 0, 10, 10), (11, 2, 14, 9)) == 0


def test_a_sign_drawn_over_a_chord_is_refused_and_one_beside_it_passes(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pdfcheck, "SIGNS", "S")

    def page(path: pathlib.Path, sign_x: float, sign_y: float) -> pathlib.Path:
        doc = pymupdf.open()
        sheet = doc.new_page()
        sheet.insert_text((100, 200), "Gm", fontsize=14)
        sheet.insert_text((sign_x, sign_y), "S", fontsize=12)
        doc.save(path)
        return path

    assert any("covers" in p for p in pdfcheck.sign_on_chord(page(tmp_path / "on.pdf", 106, 190)))
    assert not any("covers" in p for p in pdfcheck.sign_on_chord(page(tmp_path / "off.pdf", 200, 190)))


def test_a_pdf_is_parsed_once_until_it_changes(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "a.pdf"
    doc = pymupdf.open()
    doc.new_page().insert_text((50, 50), "Am", fontsize=12)
    doc.save(path)
    first = pdftext.open(path)
    assert pdftext.open(path) is first
    assert first[0].get_text("dict") is first[0].get_text("dict")
    doc = pymupdf.open()
    doc.new_page().insert_text((50, 50), "Dm7", fontsize=12)
    doc.save(path)
    assert "Dm7" in pdftext.open(path)[0].get_text("text")


def test_a_sign_over_a_chord_marks_its_line_for_a_wider_plan(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf = blank_pdf(tmp_path / "p.pdf")
    monkeypatch.setattr(pdfcheck, "chord_collisions", lambda _: [])
    monkeypatch.setattr(pdfcheck, "past_margin", lambda _: [])
    monkeypatch.setattr(pdfcheck, "sign_chord_hits", lambda _: [(1, 100.0, "sign covers a chord")])
    assert port.crowded_rows(pdf, [1]) == {1}
