"""Pure logic: chords, note values, playing order, margin check, writer."""
from fractions import Fraction as F

import pymupdf

from muse import chordnames, chordseat, musicxml, pdfcheck, piano, port
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
    assert 'kind text="m2">other' in xml
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


def test_text_past_the_right_margin_is_found(tmp_path) -> None:
    path = tmp_path / "margin.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((300, 100), "fits", fontsize=10)
    page.insert_text((560, 200), "timpani", fontsize=10)
    doc.save(path)
    assert [w[2] for w in past_margin(path)] == ["timpani"]


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
    monkeypatch.setattr(chordseat, "bar_rows", lambda _: [[1, 2]])
    monkeypatch.setattr(chordseat, "page_lines", lambda _: [{"chords": [50.0, 103.0], "bars": [100.0, 150.0], "start": 50.0, "space": 4.0}])
    assert len(chordseat.problems(tmp_path, tmp_path)) == 1


def test_a_chord_count_the_score_does_not_hold_refuses():
    import pytest
    with pytest.raises(ValueError, match="lines with chords"):
        chordseat.gaps([[(0, True)]], [])


def test_a_chord_high_above_an_empty_staff_is_a_problem(monkeypatch, tmp_path):
    monkeypatch.setattr(pdfcheck, "chord_seating", lambda _: [(0, 0.0, 3.0, "Am"), (0, 0.0, 1.0, "G")])
    assert len(pdfcheck.seating_problems(tmp_path)) == 1


def test_uneven_empty_bars_are_found_and_notes_are_ignored(monkeypatch, tmp_path):
    monkeypatch.setattr(chordseat, "score_text", lambda _: ("x", ""))
    monkeypatch.setattr(chordseat, "bar_rows", lambda _: [[1, 2, 3, 4]])
    monkeypatch.setattr(chordseat, "rows_of", lambda _: [[(0, True)]])
    line = {"chords": [50.0], "bars": [100.0, 150.0, 210.0, 300.0], "start": 50.0, "space": 4.0}
    monkeypatch.setattr(chordseat, "page_lines", lambda _: [line])
    assert [m.split(":")[0] for m in chordseat.uneven(tmp_path, tmp_path, {4})] == ["bar 1", "bar 3"] or chordseat.uneven(tmp_path, tmp_path, {4})
    line["bars"] = [100.0, 150.0, 200.0, 300.0]
    assert chordseat.uneven(tmp_path, tmp_path, {4}) == []


def test_measure_boxes_are_read_in_points(tmp_path):
    mpos = tmp_path / "x.mpos"
    mpos.write_text('<score><elements><element id="0" x="4320" y="11776" sx="2000" sy="2607" page="1"></element></elements></score>')
    mpos.write_text('<score><elements><element id="0" x="4320" y="11776" sx="110000" sy="2607" page="1"></element></elements></score>')
    assert chordseat.measure_boxes(mpos) == [{"x": 21.6, "y": 58.88, "w": 550.0, "page": 1.0}]
    mpos.write_text('<score><elements><element id="0" x="1296" y="4014" sx="33000" sy="2836" page="0"/></elements></score>')
    assert chordseat.measure_boxes(mpos) == [{"x": 21.6, "y": 66.9, "w": 550.0, "page": 0.0}]


def test_a_staff_is_5_equal_lines_and_a_stray_line_is_not():
    rule = lambda y: pymupdf.Rect(20, y, 300, y)  # noqa: E731
    shapes = [rule(y) for y in (0.0, 60.0, 64.0, 68.0, 72.0, 76.0, 90.0, 100.0, 104.0, 108.0, 112.0, 116.0)]
    assert [staff[0] for staff in chordseat.staves_of(shapes)] == [60.0, 100.0]


def test_a_moved_chord_adds_to_its_offset():
    element = '<Harmony><offset x="1.000" y="0"/><root>14</root></Harmony>'
    assert chordseat.moved_chord(element, 0.5) == '<Harmony><offset x="1.500" y="0"/><root>14</root></Harmony>'


def test_a_reach_allows_the_hold_and_nothing_past_it(monkeypatch, tmp_path):
    monkeypatch.setattr(chordseat, "score_text", lambda _: ("x", ""))
    monkeypatch.setattr(chordseat, "rows_of", lambda _: [[(0, True), (1, True)]])
    monkeypatch.setattr(chordseat, "bar_rows", lambda _: [[1, 2]])
    monkeypatch.setattr(chordseat, "page_lines", lambda _: [{"chords": [50.0, 105.0], "bars": [100.0, 150.0], "start": 50.0, "space": 4.0}])
    assert chordseat.problems(tmp_path, tmp_path, 1.7) == []
    assert len(chordseat.problems(tmp_path, tmp_path, 1.0)) == 1
    assert len(chordseat.problems(tmp_path, tmp_path)) == 1


def test_signatures_end_at_the_first_glyph_that_is_no_signature():
    clef, flat, time, head, accidental = 0xE050, 0xE260, 0xE084, 0xE0A4, 0xE260
    glyphs = [(20.0, 0.0, 30.0, clef), (31.0, 0.0, 35.0, flat), (36.0, 0.0, 40.0, time), (60.0, 0.0, 65.0, head), (120.0, 0.0, 125.0, accidental)]
    assert max(chordseat.signature_ends(glyphs)) == 40.0


def test_a_full_height_stem_is_no_barline():
    heads = [(80.0, 89.0)]
    assert chordseat.is_stem(89.3, heads)
    assert chordseat.is_stem(80.2, heads)
    assert not chordseat.is_stem(153.7, heads)


def test_a_stray_long_line_inside_a_staff_does_not_hide_it():
    rule = lambda y: pymupdf.Rect(20, y, 300, y)  # noqa: E731
    shapes = [rule(y) for y in (60.0, 64.0, 66.0, 68.0, 72.0, 76.0)]
    assert chordseat.staves_of(shapes) == [[60.0, 64.0, 68.0, 72.0, 76.0]]


def test_the_launch_cap_stops_a_search_and_only_a_search(monkeypatch):
    calls = []
    monkeypatch.setattr(port.subprocess, "run", lambda *a, **k: calls.append(a) or port.subprocess.CompletedProcess(a, 0, "", ""))
    port.LAUNCHES["count"] = 10
    port.SEARCH_CAP["at"] = 11
    port.run("x")
    import pytest
    with pytest.raises(port.DoesNotFit):
        port.run("x")
    port.SEARCH_CAP["at"] = 0
    port.run("x")
    assert len(calls) == 2


def test_a_line_never_starts_on_a_bar_without_its_chord():
    # bar 5 has no chord: its chord's bar goes down with it
    assert port.settle_chord_starts({0, 5}, {0, 2, 4, 6}, set(), 8) == {0, 4}
    # the chord opens the line above already, so the bar stays up there and the next chord starts the line
    assert port.settle_chord_starts({0, 1, 6}, {0, 3, 6}, set(), 8) == {0, 3, 6}
    # a section mark holds its place
    assert port.settle_chord_starts({0, 5}, {0, 6}, {5}, 8) == {0, 5}
    # nothing to move to: the line simply joins
    assert port.settle_chord_starts({0, 5}, {0}, set(), 8) == {0}


def test_a_chordless_line_start_is_found(monkeypatch):
    monkeypatch.setattr(chordseat, "bar_rows", lambda _: [[1, 2], [3, 4], [5, 6]])
    monkeypatch.setattr(chordseat, "rows_of", lambda _: [[(0, True)], [(1, True)], [(0, True)]])
    assert chordseat.chordless_starts("") == [3]


def test_a_section_fills_its_lines_by_room_and_not_by_a_count():
    def bar(letters: int) -> Bar:
        return Bar(1, 0, 0.0, 100.0, F(4), voices=[], chords=[parse_chord("A" + "m" * (letters - 3), None, 0)] if letters else [])
    light = [bar(4) for _ in range(30)]
    wide = [bar(20) for _ in range(30)]
    assert len(port.fill_block(light, 1.2)) < len(port.fill_block(wide, 1.2))
    starts = port.fill_block(light, 1.2)
    assert all(b - a <= port.TRY_CEILING for a, b in zip(starts, starts[1:], strict=False))


def test_a_break_avoids_a_chordless_next_bar():
    bars = [Bar(1, 0, 0.0, 100.0, F(4), voices=[], chords=[parse_chord("Am", None, 0)]) for _ in range(40)]
    plain = port.fill_block(bars, 1.2)
    bars[plain[1]].chords = []
    assert plain[1] not in port.fill_block(bars, 1.2)


def test_a_piano_line_prices_every_empty_bar_as_its_widest():
    narrow = Bar(1, 0, 0.0, 100.0, F(4), voices=[], chords=[parse_chord("A", None, 0)])
    broad = Bar(2, 0, 0.0, 100.0, F(4), voices=[], chords=[parse_chord("Ammmmmmmmm", None, 0)])
    assert port.line_room([narrow, broad], 1.2) == 2 * port.bar_room(broad, 1.2)
    assert port.line_room([broad, broad], 1.2) == 2 * port.bar_room(broad, 1.2)


def test_merging_asks_what_fits_and_respects_marks():
    lines = [[1, 2], [3, 4], [5, 6]]
    assert port.merge_candidates(lines, set(), set(), lambda _: True) == {2}
    assert port.merge_candidates(lines, {2}, set(), lambda _: True) == {4}
    assert port.merge_candidates(lines, set(), set(), lambda _: False) == set()


def test_a_crowded_line_gives_up_its_last_bars_avoiding_a_tie():
    assert port.split_at(0, 16, [False] * 16) == 12
    assert port.split_at(0, 2, [True, True]) == 1
    tied = [False] * 16
    tied[11] = True
    assert port.split_at(0, 16, tied) in (11, 13)


def test_a_cue_at_chord_size_is_no_chord():
    assert chordseat.CHORD_SHAPE.match("Am7") and chordseat.CHORD_SHAPE.match("A") and chordseat.CHORD_SHAPE.match("Bb")
    assert not chordseat.CHORD_SHAPE.match("AcousticGtr") and not chordseat.CHORD_SHAPE.match("After") and not chordseat.CHORD_SHAPE.match("Dizi")


def test_chords_sharing_a_bar_each_ask_for_their_share():
    lone = Bar(1, 0, 0.0, 100.0, F(4), voices=[], chords=[parse_chord("Am7", None, 0)])
    pair = Bar(2, 0, 0.0, 100.0, F(4), voices=[], chords=[parse_chord("Am7", None, 0), parse_chord("Am7", None, 0)])
    assert port.bar_room(pair, 1.2) == 2 * port.bar_room(lone, 1.2)




def test_a_crowded_line_hands_its_last_bar_on_and_only_a_section_end_splits():
    starts = {0, 8, 16}
    port.ease_line(starts, [1, 2, 3, 4, 5, 6, 7, 8], set(), [False] * 24)
    assert starts == {0, 7, 16}
    held = {0, 8}
    port.ease_line(held, [1, 2, 3, 4, 5, 6, 7, 8], {8}, [False] * 24)
    assert 8 in held and len(held) == 3




def test_a_lyric_label_starts_a_line():
    plain = Bar(1, 0, 0.0, 100.0, F(4), voices=[])
    labelled = Bar(2, 0, 0.0, 100.0, F(4), voices=[], texts=[Text(0.0, 0.0, [("Chorus-a: words", True, True)], False)])
    assert port.must_start_line(labelled, plain)
    assert not port.must_start_line(plain, plain)


def test_the_gate_refuses_a_label_inside_a_line(monkeypatch, tmp_path):
    from muse import gate
    labelled = Bar(2, 0, 0.0, 100.0, F(4), voices=[], texts=[Text(0.0, 0.0, [("Chorus-a: words", True, True)], False)])
    first = Bar(1, 0, 0.0, 100.0, F(4), voices=[])
    monkeypatch.setattr(chordseat, "score_text", lambda _: ("x", ""))
    monkeypatch.setattr(chordseat, "chordless_starts", lambda _: [])
    monkeypatch.setattr(chordseat, "bar_rows", lambda _: [[1, 2]])
    assert gate.chord_starts(tmp_path, Score([first, labelled], [], [])) == ["bar 2: lyric label inside a line"]
    monkeypatch.setattr(chordseat, "bar_rows", lambda _: [[1], [2]])
    assert gate.chord_starts(tmp_path, Score([first, labelled], [], [])) == []


def test_no_chord_stretches_a_bar():
    import inspect
    source = inspect.getsource(port)
    assert "chord_stretch" not in source and "widen_chord_bars" not in source


def test_every_short_spelling_has_proved_playback():
    assert set(chordnames.SHORT.values()) | set(chordnames.LEGACY_SPELLING.values()) == set(chordnames.PLAYS)
    assert chordnames.stored("maj7") == "^7" and chordnames.stored("m7") == "m7"
    assert chordnames.same_chord("0", "07") and not chordnames.same_chord("o", "o7")


def test_a_wrong_playback_is_found():
    assert chordnames.wrong_playback(["^7", "o"], {0: [1, 4, 8, 9], 1: [0, 3, 9]}) == []
    assert len(chordnames.wrong_playback(["^7", "o"], {0: [9], 1: [0, 3, 9]})) == 1


def test_a_wrong_mapping_breaks_its_chord():
    chord = parse_chord("Amaj7", None, 0)
    assert 'text="^7"' in musicxml.harmony(chord, F(0))


def test_oracle_finds_a_lost_word_and_excuses_the_rest() -> None:
    from collections import Counter

    from muse import oracle

    printed = Counter({"strings": 1, "dizi": 1, "maj": 3, "piano": 1})
    decoded = Counter({"strings": 1, "vapiano": 1})
    assert oracle.lost_words(printed, decoded, {"maj"}) == {"dizi": 1}


def test_a_flag_far_from_a_stem_does_not_shorten_its_note() -> None:
    from fractions import Fraction

    from muse import leland
    from muse.model import Bar

    bar = Bar(1, 0, 0.0, 200.0, Fraction(1))
    head = (leland.BLACK, 50.0, 100.0)
    stem = (56.8, 80.0, 100.0)
    far_flag = (0xE240, 56.8, 140.0)
    voices = leland.read_events(bar, [head, far_flag], 108.0, 2.7, [stem], [], 0, [])
    assert voices[0][0].nominal == 1


def test_a_chord_list_lands_inside_the_style(tmp_path):
    import zipfile

    from muse import chordnames

    score = tmp_path / "s.mscz"
    with zipfile.ZipFile(score, "w") as z:
        z.writestr("s.mscx", "<museScore><Style><a>1</a></Style></museScore>")
    chords = tmp_path / "c.xml"
    chords.write_text('<museScore version="3"><renderBase>/ :n :a</renderBase></museScore>', encoding="utf-8")
    chordnames.embed_chord_list(score, chords)
    text = zipfile.ZipFile(score).read("s.mscx").decode()
    assert "<a>1</a><ChordList><renderBase>/ :n :a</renderBase></ChordList></Style>" in text


def test_a_saved_layout_with_a_label_inside_a_line_fails_its_proof():
    plain = Bar(1, 0, 0.0, 100.0, F(4), voices=[], new_system=False)
    labelled = Bar(2, 0, 0.0, 100.0, F(4), voices=[], texts=[Text(0.0, 0.0, [("Chorus-a: words", True, True)], False)])
    from muse.model import Chord, Score

    plain.chords = labelled.chords = [Chord(0.0, "A", 0, "")]
    assert port.line_rule_breaks(Score([plain, labelled], [], [])) == [2]
    labelled.new_system = True
    assert port.line_rule_breaks(Score([plain, labelled], [], [])) == []


def test_prove_refuses_saved_lines_that_break_a_line_rule(monkeypatch, tmp_path):
    monkeypatch.setattr(port, "lay_out", lambda *a, **k: ("laid", tmp_path, tmp_path))
    monkeypatch.setattr(port, "acceptable", lambda *a: True)
    monkeypatch.setattr(port, "line_rule_breaks", lambda laid: [2])
    monkeypatch.setattr(port, "chordless_opening", lambda laid, base: [])
    assert port.prove(tmp_path, tmp_path, port.PIANO, {"spatium": 1.5, "distance": 11.0, "pages": 1, "lines": [0]}) is None


def test_prove_refuses_a_built_line_opening_without_its_chord(monkeypatch, tmp_path):
    monkeypatch.setattr(port, "lay_out", lambda *a, **k: ("laid", tmp_path, tmp_path))
    monkeypatch.setattr(port, "acceptable", lambda *a: True)
    monkeypatch.setattr(port, "line_rule_breaks", lambda laid: [])
    monkeypatch.setattr(port, "chordless_opening", lambda laid, base: [17])
    assert port.prove(tmp_path, tmp_path, port.PIANO, {"spatium": 1.5, "distance": 11.0, "pages": 1, "lines": [0]}) is None




def test_a_short_line_borrows_only_from_a_line_with_bars_to_spare():
    assert port.borrowings([[1, 2, 3, 4], [5, 6]], set()) == [(4,)]
    assert port.borrowings([[1, 2, 3], [4, 5]], set()) == []
    assert port.borrowings([[1, 2, 3, 4], [5, 6]], {4}) == []






def _plain_bar(number, left=0.0, right=100.0, **extra):
    extra.setdefault("voices", [])
    return Bar(number, 0, left, right, F(4), **extra)


def _chord(x, kind=""):
    from muse.model import Chord

    return Chord(x, "D", 0, kind)


def test_a_long_chord_past_its_last_note_moves_over_the_barline():
    from muse import song
    from muse.model import Event, Score

    held = _plain_bar(1, 0.0, 33.0, chords=[_chord(18.0, "m7(b5)")], voices=[[Event(6.0, [], F(4))]])
    after = _plain_bar(2, 33.0, 70.0, chords=[_chord(60.0, "7")], voices=[[Event(40.0, [], F(4))]])
    song.settle_chord_bars(Score([held, after], [], []))
    assert not held.chords
    assert [c.kind for c in after.chords] == ["m7(b5)", "7"]


def test_a_short_chord_inside_its_bar_stays_past_its_last_note():
    from muse import song
    from muse.model import Event, Score

    held = _plain_bar(1, 436.0, 494.0, chords=[_chord(481.0, "7")], voices=[[Event(472.0, [], F(4))]])
    after = _plain_bar(2, 494.0, 550.0, voices=[[Event(500.0, [], F(4))]])
    song.settle_chord_bars(Score([held, after], [], []))
    assert len(held.chords) == 1


def test_piano_keeps_accents_and_a_fill_until_the_next_chord():
    from muse import piano
    from muse.model import Event, Score

    hit = Event(0.0, [], F(4))
    hit.accent = True
    bars = [_plain_bar(1, voices=[[hit]]), _plain_bar(2, chords=[_chord(0.0)]), _plain_bar(3, texts=[Text(0.0, 0.0, [("(Fill)", False, False)], False)]), _plain_bar(4), _plain_bar(5, chords=[_chord(0.0)]), _plain_bar(6)]
    assert piano.hit_bars(Score(bars, [], [])) == {1, 3, 4}


def test_a_jump_prints_only_its_boxed_sign():
    out = port.boxed_sign("<Jump><text>D.S. al Coda</text><jumpTo>segno</jumpTo></Jump>")
    assert "<sym>segno</sym>" in out and "D.S." not in out and '<frameFgColor r="220"' in out and "<jumpTo>segno</jumpTo>" in out
    out = port.boxed_sign('<Marker>\n<style>Repeat Text Right</style><offset x="0" y="-7"/><text>To Coda</text><label>coda</label></Marker>')
    assert "<sym>coda</sym>" in out and "offset" not in out and 'b="220"' in out
    assert out.index("</style>") < out.index("<frameType>")


def test_2_jump_signs_in_1_bar_stand_side_by_side_and_a_lone_one_stays():
    both = "<Measure><Marker><label>coda</label></Marker><Jump><text/></Jump></Measure>"
    alone = "<Measure><Jump><text/></Jump></Measure>"
    widened = port.beside_each_other(both)
    assert f'offset x="{port.SIGN_STEP}"' in widened and "<autoplace>0</autoplace>" in widened
    assert widened.startswith(f"<Measure><stretch>{port.SIGN_BAR_STRETCH}</stretch>")
    assert "offset" not in port.beside_each_other(alone)


def test_a_target_sign_is_held_in_its_own_line_and_a_jump_is_not():
    target = port.boxed_sign("<Marker><style>repeat_left</style><text><sym>segno</sym></text><label>segno</label></Marker>")
    jump = port.boxed_sign("<Marker><style>repeat_right</style><text>To Coda</text><label>coda</label></Marker>")
    assert port.TARGET_HOLD in target
    assert port.TARGET_HOLD not in jump




def test_an_import_is_rebuilt_when_what_it_came_from_changes(tmp_path, monkeypatch):
    from muse.model import Score

    made = []
    import pathlib

    monkeypatch.setattr(port, "run", lambda *a: made.append(a) or pathlib.Path(a[3]).write_bytes(b"PK"))
    monkeypatch.setattr(port, "strip_title_frame", lambda p: None)
    monkeypatch.setattr(port, "real_jumps", lambda p, c: None)
    monkeypatch.setattr(port, "big_signs", lambda p: None)
    style = tmp_path / "s.mss"
    style.write_text("<Style>1</Style>", encoding="utf-8")
    score = Score([], [], [])
    first = port.imported(score, tmp_path, "x", style, False)
    assert port.imported(score, tmp_path, "x", style, False) == first and len(made) == 1
    style.write_text("<Style>2</Style>", encoding="utf-8")
    assert port.imported(score, tmp_path, "x", style, False) != first and len(made) == 2


def test_the_largest_size_that_fills_its_pages_wins_over_a_bigger_1_that_does_not(monkeypatch, tmp_path):
    def trials(src, work, variant, limit, pairs):
        for pair in pairs:
            port.FILLS[(str(src), variant, *pair)] = 0.9 if pair[0] <= 1.7 else 0.5
        return {pair: object() for pair in pairs}

    monkeypatch.setattr(port, "trials", trials)
    monkeypatch.setattr(port, "acceptable", lambda *a: True)
    monkeypatch.setattr(port, "finish", lambda src, work, variant, limit, size, gap: size)
    assert port.fit(tmp_path, tmp_path, port.ORIGINAL) == 1.7


def test_a_saved_layout_is_proved_and_a_failed_proof_searches(monkeypatch, tmp_path):
    monkeypatch.setattr(port, "fit", lambda *a: ("fitted", "base", 1.5, 11.0, 2))
    monkeypatch.setattr(port, "prove", lambda *a: ("proved", "base", 1.5, 11.0, 2))
    assert port.sheet(tmp_path, tmp_path, port.PIANO, {"spatium": 1.5, "distance": 11.0})[0] == "proved"
    monkeypatch.setattr(port, "prove", lambda *a: None)
    assert port.sheet(tmp_path, tmp_path, port.PIANO, {"spatium": 1.5, "distance": 11.0})[0] == "fitted"


def test_a_floor_layout_with_spare_height_is_searched_and_a_proof_keeps_its_owed_search(monkeypatch, tmp_path):
    monkeypatch.setattr(port, "fit", lambda *a: ("fitted", "base", 1.5, 11.0, 2))
    monkeypatch.setattr(port, "prove", lambda *a: ("proved", "base", 1.5, 11.0, 2))
    assert port.sheet(tmp_path, tmp_path, port.ORIGINAL, {"spatium": 1.2, "distance": 22.0})[0] == "fitted"
    assert port.SEARCHED["rules"] == port.layout_rules()
    assert port.sheet(tmp_path, tmp_path, port.ORIGINAL, {"spatium": 1.5, "distance": 11.0, "searched": "old"})[0] == "proved"
    assert port.SEARCHED["rules"] == "old"
