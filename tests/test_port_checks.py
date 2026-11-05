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


def bass_pdf(path, *parts):
    doc = pymupdf.open()
    page = doc.new_page()
    for x, y, size, text in parts:
        page.insert_text((x, y), text, fontsize=size)
    doc.save(path)
    return path


def test_a_bass_on_the_roots_baseline_and_size_passes(tmp_path):
    assert pdfcheck.bass_problems(bass_pdf(tmp_path / "b.pdf", (50, 100, 14, "A/C"))) == []


def test_a_bass_set_apart_from_its_root_is_refused(tmp_path):
    assert pdfcheck.bass_problems(bass_pdf(tmp_path / "b.pdf", (50, 100, 14, "A"), (70, 140, 10, "/C")))


def test_a_lyric_label_with_a_slash_is_no_bass(tmp_path):
    assert pdfcheck.bass_problems(bass_pdf(tmp_path / "b.pdf", (50, 100, 10, "/Perc"))) == []


def test_the_chord_file_keeps_the_bass_level():
    assert any("/ :n :a" in new for _, new in chordnames.CHORD_FILE_RENDERS)


def test_a_bass_a_little_lower_on_its_own_line_is_refused(tmp_path):
    found = pdfcheck.bass_problems(bass_pdf(tmp_path / "b.pdf", (50, 100, 14, "A"), (58, 101.5, 14, "/C")))
    assert found
    assert "baselines" in found[0]


def test_a_jump_without_its_box_or_with_words_is_refused(tmp_path):
    import zipfile

    from muse import scorefile

    for body, found in (
        ('<Jump><text>D.S.</text></Jump>', "words"),
        ('<Jump><text><sym>segno</sym></text></Jump>', "no box"),
        ('<Jump><frameType>1</frameType><frameFgColor r="0" g="0" b="220" a="255"/><text><sym>segno</sym></text></Jump>', "wrong box"),
    ):
        path = tmp_path / "s.mscz"
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("s.mscx", f'<museScore version="4.70"><Score>{body}</Score></museScore>')
        assert any(found in problem for problem in scorefile.builtin_sign_problems(path))


def _row_pdf(path, sign_y):
    doc = pymupdf.open()
    page = doc.new_page()
    for top in (100.0, 200.0):
        for k in range(5):
            page.draw_line((40, top + 2 * k), (500, top + 2 * k))
    page.insert_text((60, 185), "Am7", fontsize=11)
    page.insert_text((450, sign_y), "S", fontsize=14)
    doc.save(path)
    return path


def test_a_jump_sign_above_its_chord_row_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(pdfcheck, "SIGNS", "S")
    assert pdfcheck.sign_row_problems(_row_pdf(tmp_path / "high.pdf", 150))
    assert pdfcheck.sign_row_problems(_row_pdf(tmp_path / "row.pdf", 186)) == []
