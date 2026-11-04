"""Pure logic: chords, note values, playing order, margin check, writer."""
from fractions import Fraction as F

import pymupdf

from muse import musicxml, pdfcheck, piano, port
from muse.model import Bar, Chord, Event, Head, Score, Text
from muse.navigate import expected_order
from muse.pdfcheck import chord_collisions, foreign_fonts, idle_staves, margin_words, oversized_text, past_margin, title_problems
from muse.symbolfont import Decoder, assemble_chords, is_dust, nominal_dots, parse_chord
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


def test_text_past_the_right_margin_is_found(tmp_path) -> None:
    path = tmp_path / "margin.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((300, 100), "fits", fontsize=10)
    page.insert_text((560, 200), "timpani", fontsize=10)
    doc.save(path)
    assert [w[2] for w in past_margin(path)] == ["timpani"]


def test_bars_with_more_chords_get_more_width() -> None:
    assert port.chord_stretch(1) == 1.0
    assert port.chord_stretch(3) > port.chord_stretch(2) > 1.0


def test_short_neighbours_merge_unless_fixed_or_refused() -> None:
    lines = [[1, 2], [3, 4], [5, 6, 7, 8, 9, 10], [11, 12]]
    assert port.merge_candidates(lines, set(), set()) == {2, 10}
    assert port.merge_candidates(lines, {2}, set()) == {4}
    assert port.merge_candidates(lines, {2, 4}, {10}) == set()


def test_short_line_share_counts_lines_of_2_bars_or_fewer() -> None:
    assert port.short_line_share([[1, 2], [3, 4, 5, 6], [7], [8, 9, 10, 11]]) == 0.5


def test_beam_pieces_at_1_height_are_1_level() -> None:
    from muse.symbolfont import beam_levels
    assert beam_levels([10.0, 10.2, 13.0]) == 2


def test_voice_split_keeps_coincident_events_apart() -> None:
    from muse.symbolfont import voice_split
    a = Event(0.0, [Head(0.0, 0.0, "C", 5)], F(1))
    b = Event(0.5, [Head(0.5, 0.0, "D", 5)], F(3), 1)
    c = Event(5.0, [Head(5.0, 0.0, "E", 5)], F(3), 1)
    d = Event(6.0, [Head(6.0, 0.0, "F", 5)], F(1))
    split = voice_split([a, b, c, d], [(a, b)], F(4))
    assert split is not None
    assert (a in split[0]) != (b in split[0])


def test_stem_split_drops_a_rest_stamped_under_a_note() -> None:
    from muse.symbolfont import stem_split
    up = Event(0.0, [Head(0.0, 0.0, "C", 5)], F(4), stem="up")
    down = Event(1.0, [Head(1.0, 0.0, "D", 4)], F(4), stem="down")
    stray = Event(1.5, [], F(1))
    split = stem_split([up, down, stray], F(4))
    assert split == ([up], [down])


def test_a_tempo_decimal_printed_in_2_spans_is_read_whole() -> None:
    class S:
        def __init__(self, text: str) -> None:
            self.text = text

    assert Decoder.tempo_value([S("Slow = 52"), S(".5")]) == 52.5
    assert Decoder.tempo_value([S("Slow = 43"), S("86")]) == 43


def _bar(number: int, notes: bool, coda: bool = False, label: str = "") -> Bar:
    heads = [Head(0.0, 0.0, "C", 5)] if notes else []
    texts = [Text(0.0, 0.0, [(label, False, True)], False)] if label else []
    return Bar(number, 0, 0.0, 1.0, F(4), voices=[[Event(0.0, heads, F(4))]], coda=coda, texts=texts)


def test_an_unlabelled_coda_keeps_only_its_closing_run() -> None:
    bars = [_bar(1, True), _bar(2, False, label="Verse1: words"), _bar(3, False)]
    bars += [_bar(4, True, coda=True), _bar(5, False), _bar(6, True), _bar(7, True), _bar(8, True)]
    found = {(p.name, p.first, p.last) for p in piano.parts(Score(bars, [], []))}
    assert ("outro", 6, 8) in found and ("intro", 1, 1) in found
    assert not any(first <= 4 <= last for _, first, last in found)


def test_a_kept_bar_takes_notes_from_the_other_staff() -> None:
    bar = _bar(1, True)
    bar.voices2 = []
    part_two = [_bar(2, False), bar]
    part_two[0].voices2 = [[Event(0.0, [Head(0.0, 0.0, "D", 5)], F(4))]]
    bars = [_bar(1, False), *part_two]
    bars[1].number, bars[2].number = 2, 3
    bars[0].texts = [Text(0.0, 0.0, [("Verse1: words", False, True)], False)]
    bars[1].rehearsal = ["Interlude"]
    bars[1].texts2 = [Text(0.0, 0.0, [("PIANO ONLY", True, False)], False)]
    score = Score(bars, [], [], has_second_staff=True)
    piano.reduce(score)
    assert any(e.heads for v in score.bars[2].voices for e in v)


def test_a_line_of_2_staves_refuses_the_one_without_notes() -> None:
    assert idle_staves([100.0, 150.0], [95.0, 148.0], []) == []
    assert idle_staves([100.0, 150.0], [95.0], []) == [150.0]
    assert idle_staves([100.0], [], []) == []
    assert idle_staves([100.0, 300.0], [95.0], [200.0]) == []


def test_a_cue_leaves_a_staff_with_no_notes() -> None:
    bar = _bar(1, True)
    bar.voices2 = [[Event(0.0, [], F(4))]]
    bar.texts2 = [Text(0.0, 0.0, [("PIANO ONLY", True, False)], False)]
    port.lift_idle_cues(Score([bar], [], []))
    assert bar.texts2 == [] and bar.texts


def test_a_lone_dot_is_dust_and_a_cue_is_not():
    assert is_dust(".") and is_dust("..")
    assert not is_dust("(Fill)") and not is_dust("N.C")


def test_a_sign_touching_a_chord_is_found_only_on_its_baseline():
    letter = [(30.0, 40.0, 100.0)]
    assert pdfcheck.sign_touches(20.0, 100.0, 17.0, 13.0, letter)
    assert not pdfcheck.sign_touches(20.0, 92.0, 17.0, 13.0, letter)  # raised above the letters
    assert not pdfcheck.sign_touches(0.0, 100.0, 17.0, 13.0, letter)  # clear to the left


def test_the_page_check_includes_the_sign_guard(tmp_path, monkeypatch):
    path = tmp_path / "plain.pdf"
    doc = pymupdf.open()
    doc.new_page().insert_text((100, 100), "Am", fontsize=12)
    doc.save(path)
    monkeypatch.setattr(pdfcheck, "sign_on_chord", lambda _: ["planted"])
    assert "planted" in pdfcheck.all_problems(path, "x")


def test_beat_1_chords_measure_from_their_own_bar_and_later_chords_do_not_count():
    rows = [[(0, True), (1, True), (1, False)]]
    lines = [{"chords": [50.0, 100.0, 130.0], "bars": [100.0, 150.0], "start": 50.0, "space": 4.0}]
    assert chordseat.gaps(rows, lines) == [0.0, 0.0, None]


def test_a_chord_off_its_barline_is_a_problem(monkeypatch, tmp_path):
    monkeypatch.setattr(chordseat, "score_text", lambda _: ("x", ""))
    monkeypatch.setattr(chordseat, "rows_of", lambda _: [[(0, True), (1, True)]])
    monkeypatch.setattr(chordseat, "page_lines", lambda _: [{"chords": [50.0, 103.0], "bars": [100.0], "start": 50.0, "space": 4.0}])
    assert len(chordseat.problems(tmp_path, tmp_path)) == 1


def test_a_chord_count_the_score_does_not_hold_refuses():
    import pytest
    with pytest.raises(ValueError, match="lines with chords"):
        chordseat.gaps([[(0, True)]], [])


def test_a_chord_high_above_an_empty_staff_is_a_problem(monkeypatch, tmp_path):
    monkeypatch.setattr(pdfcheck, "chord_seating", lambda _: [(0, 0.0, 3.0, "Am"), (0, 0.0, 1.0, "G")])
    assert len(pdfcheck.seating_problems(tmp_path)) == 1
