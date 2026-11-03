"""Pure logic: chords, note values, playing order, margin check, writer."""
from fractions import Fraction as F

import pymupdf

from muse import musicxml, port
from muse.model import Bar, Chord, Event, Head, Score
from muse.navigate import expected_order
from muse.pdfcheck import chord_collisions, foreign_fonts, margin_words, oversized_text, title_problems
from muse.symbolfont import Decoder, assemble_chords, nominal_dots, parse_chord
from muse.verify import midi


def test_parse_chord_root_kind_bass():
    c = parse_chord("Bbmaj7/D", None, 0)
    assert (c.root, c.root_alter, c.kind, c.bass, c.bass_alter) == ("B", -1, "maj7", "D", 0)


def test_slash_alone_keeps_previous_chord():
    prev = parse_chord("Am7", None, 0)
    c = parse_chord("/G", prev, 9)
    assert (c.root, c.kind, c.bass) == ("A", "m7", "G")


def test_assemble_keeps_adjacent_slash_and_splits_far_one():
    assert [t for _, t in assemble_chords([(0, 5, "C"), (5, 7, "7"), (7.5, 9, "/B"), (9, 10, "b")])] == ["C7/Bb"]
    assert [t for _, t in assemble_chords([(0, 5, "Am"), (20, 25, "/G")])] == ["Am", "/G"]


def test_nominal_dots():
    assert nominal_dots(F(3, 2))[1] == 1
    assert nominal_dots(F(1))[1] == 0


def test_midi():
    assert midi("C", 0, 4) == 60
    assert midi("B", -1, 3) == 58


def bar(n, **kw):
    b = Bar(n, 0, 0, 100, F(4))
    for k, v in kw.items():
        setattr(b, k, v)
    return b


def test_order_repeat_volta():
    s = Score([bar(1, repeat_start=True), bar(2, volta="1", repeat_end=True), bar(3, volta="2"), bar(4)], [], [])
    assert expected_order(s) == [1, 2, 1, 3, 4]


def test_order_ds_al_coda_skips_repeat_then_coda_repeat_plays():
    s = Score([bar(1), bar(2, segno=True), bar(3, to_coda=True), bar(4, dal_segno=True), bar(5, coda=True, repeat_start=True), bar(6, repeat_end=True)], [], [])
    assert expected_order(s) == [1, 2, 3, 4, 2, 3, 5, 6, 5, 6]


def test_margin_check_catches_planted_page_number(tmp_path):
    clean = tmp_path / "clean.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((100, 400), "music")
    doc.save(clean)
    assert margin_words(clean) == []
    dirty = tmp_path / "dirty.pdf"
    page.insert_text((300, 835), "Footer")
    doc.save(dirty)
    assert margin_words(dirty) == ["p1:Footer"]


def test_margin_check_catches_page_number_above_margin(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((300, 790), "1")
    path = tmp_path / "n.pdf"
    doc.save(path)
    assert margin_words(path) == ["p1:1"]


def test_two_rhythms_split_when_long_note_has_no_stem():
    d = Decoder.__new__(Decoder)
    d.threes, d.flags, d.corrections, d.current_base = [], [], [], 0.0
    b = Bar(1, 0, 0, 100, F(4))
    whole = Event(10, [Head(10, 0, "D", 5)], F(4))
    up = Event(10, [Head(10, 9, "G", 4)], F(2), stem="up")
    down = Event(60, [Head(60, 12, "E", 4)], F(2), stem="down")
    voices = d.voices(b, [whole, up, down], False, [])
    assert len(voices) == 2 and d.flags == []


def test_writer_durations_and_chord_offset():
    head = Head(10, 0, "C", 5, alter=0)
    b = Bar(1, 0, 0, 100, F(4), key=0, time=(4, 4))
    b.voices = [[Event(10, [head], F(4), onset=F(0))]]
    b.chords = [Chord(10, "C", 0, "m(add2)"), Chord(55, "G", 0, "7")]
    xml = musicxml.write(Score([b], [], []))
    assert "<duration>192</duration>" in xml
    assert 'kind text="m(add2)">other' in xml
    assert "<offset>96</offset>" in xml


def _row_score(count: int, tied_after: set[int]) -> Score:
    bars = []
    for number in range(1, count + 1):
        head = Head(0.0, 0.0, "C", 5, tie_start=number in tied_after)
        bars.append(Bar(number, 0, 0.0, 1.0, F(4), voices=[[Event(0.0, [head], F(4))]]))
    return Score(bars, [], [])



def test_tie_inside_a_bar_does_not_leave_it() -> None:
    first = Event(0.0, [Head(0.0, 0.0, "C", 5, tie_start=True)], F(2))
    last = Event(1.0, [Head(1.0, 0.0, "C", 5, tie_stop=True)], F(2))
    assert not port.tied_out(Bar(1, 0, 0.0, 1.0, F(4), voices=[[first, last]]))
    last.heads[0].tie_start = True
    assert port.tied_out(Bar(1, 0, 0.0, 1.0, F(4), voices=[[first, last]]))


def test_pages_split_as_evenly_as_possible() -> None:
    assert port.balanced_pages([10.0] * 6, 2) == [3]
    assert port.balanced_pages([30.0, 10.0, 10.0, 10.0], 2) == [1]
    assert port.balanced_pages([10.0] * 6, 3) == [2, 4]


def test_largest_fit_finds_the_edge_in_few_tries() -> None:
    tried = []

    def fits(value: float) -> bool:
        tried.append(value)
        return value <= 2.2

    assert port.largest_fit(1.6, 0.05, 3.0, fits) == 2.2
    assert len(tried) <= 6


def test_font_check_refuses_a_fallback_font(tmp_path) -> None:
    path = tmp_path / "fallback.pdf"
    doc = pymupdf.open()
    doc.new_page().insert_text((100, 400), "word", fontname="helv")
    doc.save(path)
    assert foreign_fonts(path) == ["Helvetica"]


def test_chords_must_be_the_biggest_text(tmp_path) -> None:
    good = tmp_path / "good.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((100, 100), "Am7", fontsize=14)
    page.insert_text((100, 200), "Strings", fontsize=11)
    doc.save(good)
    page.insert_text((200, 100), "Dm add2", fontsize=14)
    page.insert_text((300, 100), "maj7", fontsize=14)
    doc.save(good)
    assert oversized_text(good) == []
    bad = tmp_path / "bad.pdf"
    page.insert_text((100, 300), "Strings", fontsize=14)
    doc.save(bad)
    assert oversized_text(bad) == ["Strings"]


def test_chords_running_together_are_found(tmp_path) -> None:
    path = tmp_path / "chords.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((100, 100), "Am7", fontsize=14)
    width = pymupdf.get_text_length("Am7", fontsize=14)
    page.insert_text((100 + width + 0.5, 100), "Dm7", fontsize=14)
    page.insert_text((300, 100), "G7", fontsize=14)
    doc.save(path)
    assert [c[2] for c in chord_collisions(path)] == ["D"]


def test_title_must_print_once_at_the_top_right(tmp_path) -> None:
    path = tmp_path / "title.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((500, 30), "Song", fontsize=9)
    doc.save(path)
    assert title_problems(path, "Song") == []
    other = pymupdf.open()
    other.new_page(width=595, height=842).insert_text((100, 400), "Song", fontsize=9)
    moved = tmp_path / "moved.pdf"
    other.save(moved)
    assert title_problems(moved, "Song") == ["p1: title printed 0 times at the top right"]
    missing = pymupdf.open()
    missing.new_page(width=595, height=842).insert_text((100, 400), "music", fontsize=9)
    absent = tmp_path / "absent.pdf"
    missing.save(absent)
    assert title_problems(absent, "Song") == ["p1: title printed 0 times at the top right"]


def test_grid_keeps_a_steady_line_length() -> None:
    assert port.grid(8, 4, [False] * 8) == [0, 4]
    assert port.grid(9, 4, [False] * 9) == [0, 4]
    assert port.grid(9, 4, [False] * 9, pickup=True) == [0, 5]


def test_grid_moves_a_break_off_a_tie() -> None:
    tied = [False, False, False, True, False, False, False, False]
    assert port.grid(8, 4, tied) == [0, 5]
    assert port.grid(4, 1, [False, False, False, False]) == [0, 1, 2, 3]


def test_only_the_crowded_line_splits_near_its_middle_off_a_tie() -> None:
    assert port.split_at(0, 8, [False] * 8) == 4
    assert port.split_at(0, 8, [False, False, False, True, False, False, False, False]) == 3
    assert port.split_at(0, 2, [True, True]) == 1
    assert port.split_at(0, 6, [False, True, True, True, True, True]) == 3
