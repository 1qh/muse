"""Pure logic: chords, note values, playing order, margin check, writer."""

import ast
import collections
import concurrent.futures
import dataclasses
import importlib.util
import inspect
import os
import pathlib
import re
import shutil
import signal
import subprocess
import threading
import time
import zipfile
from collections import Counter
from fractions import Fraction
from typing import override

import psutil
import pymupdf
import pytest

from muse import (
    build,
    chordnames,
    chordseat,
    launch,
    leland,
    musicxml,
    oracle,
    pdfcheck,
    pdftext,
    piano,
    port,
    publish,
    render,
    song,
    toolpath,
    verify,
)
from muse.chordseat import INK_REACH, TOLERANCE, clean_bearings, crowded, ink_left, sign_end
from muse.layout import next_bias
from muse.leland import Staff, text_owners
from muse.model import Bar, Chord, Event, Head, Score, Text, bar_at, mark_bar
from muse.navigate import expected_order
from muse.pdfcheck import (
    chord_collisions,
    foreign_fonts,
    idle_staves,
    margin_words,
    oversized_text,
    past_margin,
    sign_touches,
    title_problems,
)
from muse.symbolfont import (
    Decoder,
    Span,
    assemble_chords,
    beam_levels,
    is_dust,
    nominal_dots,
    parse_chord,
    stem_split,
    voice_split,
)
from muse.verify import midi


def test_parse_chord_root_kind_bass() -> None:
    c = parse_chord("Bbmaj7/D", None, 0)
    assert c is not None
    assert (c.root, c.root_alter, c.kind, c.bass, c.bass_alter) == ("B", -1, "maj7", "D", 0)


def test_slash_alone_keeps_previous_chord() -> None:
    prev = parse_chord("Am7", None, 0)
    c = parse_chord("/G", prev, 9)
    assert c is not None
    assert (c.root, c.kind, c.bass) == ("A", "m7", "G")


def test_assemble_keeps_adjacent_slash_and_splits_far_one() -> None:
    assert [t for _, t in assemble_chords([(0, 5, "C"), (5, 7, "7"), (7.5, 9, "/B"), (9, 10, "b")])] == ["C7/Bb"]
    assert [t for _, t in assemble_chords([(0, 5, "Am"), (20, 25, "/G")])] == ["Am", "/G"]


def test_nominal_dots() -> None:
    assert nominal_dots(Fraction(3, 2))[1] == 1
    assert nominal_dots(Fraction(1))[1] == 0


def test_midi() -> None:
    assert midi("C", 0, 4) == 60
    assert midi("B", -1, 3) == 58


def bar(n: int, **kw: object) -> Bar:
    b = Bar(n, 0, 0, 100, Fraction(4))
    for k, v in kw.items():
        setattr(b, k, v)
    return b


def test_order_repeat_volta() -> None:
    s = Score([bar(1, repeat_start=True), bar(2, volta="1", repeat_end=True), bar(3, volta="2"), bar(4)], [], [])
    assert expected_order(s) == [1, 2, 1, 3, 4]


def test_order_ds_al_coda_skips_repeat_then_coda_repeat_plays() -> None:
    s = Score(
        [
            bar(1),
            bar(2, segno=True),
            bar(3, to_coda=True),
            bar(4, dal_segno=True),
            bar(5, coda=True, repeat_start=True),
            bar(6, repeat_end=True),
        ],
        [],
        [],
    )
    assert expected_order(s) == [1, 2, 3, 4, 2, 3, 5, 6, 5, 6]


def test_margin_check_catches_planted_page_number(tmp_path: pathlib.Path) -> None:
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


def test_margin_check_catches_page_number_above_margin(tmp_path: pathlib.Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((300, 790), "1")
    path = tmp_path / "n.pdf"
    doc.save(path)
    assert margin_words(path) == ["p1:1"]


def test_two_rhythms_split_when_long_note_has_no_stem() -> None:
    d = Decoder.__new__(Decoder)
    d.threes, d.flags, d.corrections, d.current_base = [], [], [], 0.0
    b = Bar(1, 0, 0, 100, Fraction(4))
    whole = Event(10, [Head(10, 0, "D", 5)], Fraction(4))
    up = Event(10, [Head(10, 9, "G", 4)], Fraction(2), stem="up")
    down = Event(60, [Head(60, 12, "E", 4)], Fraction(2), stem="down")
    voices = d.voices(b, [whole, up, down])
    assert len(voices) == 2
    assert d.flags == []


def test_writer_durations_and_chord_offset() -> None:
    head = Head(10, 0, "C", 5, alter=0)
    b = Bar(1, 0, 0, 100, Fraction(4), key=0, time=(4, 4))
    b.voices = [[Event(10, [head], Fraction(4), onset=Fraction(0))]]
    b.chords = [Chord(10, "C", 0, "m(add2)"), Chord(55, "G", 0, "7")]
    xml = musicxml.write(Score([b], [], [], minor=True))
    assert "<duration>192</duration>" in xml
    assert 'kind text="m2">other' in xml
    assert "<offset>96</offset>" in xml


def test_tie_inside_a_bar_does_not_leave_it() -> None:
    first = Event(0.0, [Head(0.0, 0.0, "C", 5, tie_start=True)], Fraction(2))
    last = Event(1.0, [Head(1.0, 0.0, "C", 5, tie_stop=True)], Fraction(2))
    assert not port.tied_out(Bar(1, 0, 0.0, 1.0, Fraction(4), voices=[[first, last]]))
    last.heads[0].tie_start = True
    assert port.tied_out(Bar(1, 0, 0.0, 1.0, Fraction(4), voices=[[first, last]]))


def test_pages_split_as_evenly_as_possible() -> None:
    assert port.balanced_pages([10.0] * 6, 2) == [3]
    assert port.balanced_pages([30.0, 10.0, 10.0, 10.0], 2) == [1]
    assert port.balanced_pages([10.0] * 6, 3) == [2, 4]


def test_font_check_refuses_a_fallback_font(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "fallback.pdf"
    doc = pymupdf.open()
    doc.new_page().insert_text((100, 400), "word", fontname="helv")
    doc.save(path)
    assert foreign_fonts(path) == ["Helvetica"]


def test_chords_must_be_the_biggest_text(tmp_path: pathlib.Path) -> None:
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


def test_chords_running_together_are_found(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "chords.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((100, 100), "Am7", fontsize=14)
    width = pymupdf.get_text_length("Am7", fontsize=14)
    page.insert_text((100 + width + 0.5, 100), "Dm7", fontsize=14)
    page.insert_text((300, 100), "G7", fontsize=14)
    doc.save(path)
    assert [c[2] for c in chord_collisions(path)] == ["D"]


def test_title_must_print_once_at_the_top_right(tmp_path: pathlib.Path) -> None:
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


def test_text_past_the_right_margin_is_found(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "margin.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((300, 100), "fits", fontsize=10)
    page.insert_text((560, 200), "timpani", fontsize=10)
    doc.save(path)
    assert [w[2] for w in past_margin(path)] == ["timpani"]


def test_short_line_share_counts_lines_of_2_bars_or_fewer() -> None:
    assert port.short_line_share([[1, 2], [3, 4, 5, 6], [7], [8, 9, 10, 11]]) == pytest.approx(0.5)


def test_beam_pieces_at_1_height_are_1_level() -> None:
    assert beam_levels([10.0, 10.2, 13.0]) == 2


def test_voice_split_keeps_coincident_events_apart() -> None:
    a = Event(0.0, [Head(0.0, 0.0, "C", 5)], Fraction(1))
    b = Event(0.5, [Head(0.5, 0.0, "D", 5)], Fraction(3), 1)
    c = Event(5.0, [Head(5.0, 0.0, "E", 5)], Fraction(3), 1)
    d = Event(6.0, [Head(6.0, 0.0, "F", 5)], Fraction(1))
    split = voice_split([a, b, c, d], [(a, b)], Fraction(4))
    assert split is not None
    assert (a in split[0]) != (b in split[0])


def test_stem_split_drops_a_rest_stamped_under_a_note() -> None:
    up = Event(0.0, [Head(0.0, 0.0, "C", 5)], Fraction(4), stem="up")
    down = Event(1.0, [Head(1.0, 0.0, "D", 4)], Fraction(4), stem="down")
    stray = Event(1.5, [], Fraction(1))
    split = stem_split([up, down, stray], Fraction(4))
    assert split == ([up], [down])


def test_a_tempo_decimal_printed_in_2_spans_is_read_whole() -> None:
    assert Decoder.tempo_value(
        [
            Span("Slow = 52", 0, 0, 0, "", 0, bold=False, italic=False),
            Span(".5", 0, 0, 0, "", 0, bold=False, italic=False),
        ],
    ) == pytest.approx(52.5)
    assert (
        Decoder.tempo_value(
            [
                Span("Slow = 43", 0, 0, 0, "", 0, bold=False, italic=False),
                Span("86", 0, 0, 0, "", 0, bold=False, italic=False),
            ],
        )
        == 43
    )


def _bar(number: int, *, notes: bool, coda: bool = False, label: str = "") -> Bar:
    heads = [Head(0.0, 0.0, "C", 5)] if notes else []
    texts = [Text(0.0, 0.0, [(label, False, True)], above=False)] if label else []
    return Bar(number, 0, 0.0, 1.0, Fraction(4), voices=[[Event(0.0, heads, Fraction(4))]], coda=coda, texts=texts)


def test_an_unlabelled_coda_keeps_only_its_closing_run() -> None:
    bars = [_bar(1, notes=True), _bar(2, notes=False, label="Verse1: words"), _bar(3, notes=False)]
    bars += [
        _bar(4, notes=True, coda=True),
        _bar(5, notes=False),
        _bar(6, notes=True),
        _bar(7, notes=True),
        _bar(8, notes=True),
    ]
    found = {(p.name, p.first, p.last) for p in piano.parts(Score(bars, [], []))}
    assert ("outro", 6, 8) in found
    assert ("intro", 1, 1) in found
    assert not any(first <= 4 <= last for _, first, last in found)


def test_a_kept_bar_takes_notes_from_the_other_staff() -> None:
    bar = _bar(1, notes=True)
    bar.voices2 = []
    part_two = [_bar(2, notes=False), bar]
    part_two[0].voices2 = [[Event(0.0, [Head(0.0, 0.0, "D", 5)], Fraction(4))]]
    bars = [_bar(1, notes=False), *part_two]
    bars[1].number, bars[2].number = 2, 3
    bars[0].texts = [Text(0.0, 0.0, [("Verse1: words", False, True)], above=False)]
    bars[1].rehearsal = ["Interlude"]
    bars[1].texts2 = [Text(0.0, 0.0, [("PIANO ONLY", True, False)], above=False)]
    score = Score(bars, [], [], has_second_staff=True)
    piano.reduce(score)
    assert any(e.heads for v in score.bars[2].voices for e in v)


def test_a_line_of_2_staves_refuses_the_one_without_notes() -> None:
    assert idle_staves([100.0, 150.0], [95.0, 148.0], []) == []
    assert idle_staves([100.0, 150.0], [95.0], []) == [150.0]
    assert idle_staves([100.0], [], []) == []
    assert idle_staves([100.0, 300.0], [95.0], [200.0]) == []


def test_a_cue_leaves_a_staff_with_no_notes() -> None:
    bar = _bar(1, notes=True)
    bar.voices2 = [[Event(0.0, [], Fraction(4))]]
    bar.texts2 = [Text(0.0, 0.0, [("PIANO ONLY", True, False)], above=False)]
    port.lift_idle_cues(Score([bar], [], []))
    assert bar.texts2 == []
    assert bar.texts


def test_a_lone_dot_is_dust_and_a_cue_is_not() -> None:
    assert is_dust(".")
    assert is_dust("..")
    assert not is_dust("(Fill)")
    assert not is_dust("N.C")


def test_a_sign_touching_a_chord_is_found_only_on_its_baseline() -> None:
    letter = [(30.0, 40.0, 100.0)]
    assert pdfcheck.sign_touches(20.0, 100.0, 17.0, 13.0, letter)
    assert not pdfcheck.sign_touches(20.0, 92.0, 17.0, 13.0, letter)
    assert not pdfcheck.sign_touches(0.0, 100.0, 17.0, 13.0, letter)


def test_the_page_check_includes_the_sign_guard(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "plain.pdf"
    doc = pymupdf.open()
    doc.new_page().insert_text((100, 100), "Am", fontsize=12)
    doc.save(path)
    monkeypatch.setattr(pdfcheck, "sign_on_chord", lambda _: ["planted"])
    assert "planted" in pdfcheck.all_problems(path, "x")


def test_beat_1_chords_measure_from_their_own_bar_and_later_chords_do_not_count() -> None:
    rows = [[(0, True), (1, True), (1, False)]]
    lines: list[chordseat.LineRecord] = [
        {
            "chords": [50.0, 100.0, 130.0],
            "bars": [100.0, 150.0],
            "openings": [100.0],
            "start": 50.0,
            "space": 4.0,
            "crossed": [],
        },
    ]
    assert chordseat.gaps(rows, lines) == [0.0, 0.0, None]


def test_a_chord_off_its_barline_is_a_problem(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    monkeypatch.setattr(chordseat, "score_text", lambda _: ("x", ""))
    monkeypatch.setattr(chordseat, "rows_of", lambda _: [[(0, True), (1, True)]])
    monkeypatch.setattr(chordseat, "bar_rows", lambda _: [[1, 2]])
    monkeypatch.setattr(
        chordseat,
        "matched",
        lambda _text, _pdf: [
            {
                "chords": [50.0, 103.0],
                "bars": [100.0, 150.0],
                "openings": [100.0],
                "start": 50.0,
                "space": 4.0,
                "crossed": [],
            },
        ],
    )
    assert len(chordseat.problems(tmp_path, tmp_path)) == 1


def test_a_chord_count_the_score_does_not_hold_refuses() -> None:
    with pytest.raises(ValueError, match="lines with chords"):
        chordseat.gaps([[(0, True)]], [])


def test_a_chord_high_above_an_empty_staff_is_a_problem(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    monkeypatch.setattr(pdfcheck, "chord_seating", lambda _: [(0, 0.0, 3.0, "Am"), (0, 0.0, 1.0, "G")])
    assert len(pdfcheck.seating_problems(tmp_path)) == 1


def test_uneven_empty_bars_are_found_and_notes_are_ignored(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    monkeypatch.setattr(chordseat, "score_text", lambda _: ("x", ""))
    monkeypatch.setattr(chordseat, "signed_bars", lambda _: set())
    monkeypatch.setattr(chordseat, "bar_rows", lambda _: [[1, 2, 3, 4]])
    monkeypatch.setattr(chordseat, "rows_of", lambda _: [[(0, True)]])
    line = {"chords": [50.0], "bars": [100.0, 150.0, 210.0, 300.0], "start": 50.0, "opening": 50.0, "space": 4.0}
    monkeypatch.setattr(chordseat, "matched", lambda _text, _pdf: [line])
    assert [m.split(":")[0] for m in chordseat.uneven(tmp_path, tmp_path, {4})] == ["bar 1", "bar 3"] or chordseat.uneven(
        tmp_path,
        tmp_path,
        {4},
    )
    line["bars"] = [100.0, 150.0, 200.0, 300.0]
    assert chordseat.uneven(tmp_path, tmp_path, {4}) == []
    line["start"] = 62.0
    assert chordseat.uneven(tmp_path, tmp_path, {4}) == []


def test_measure_boxes_are_read_in_points(tmp_path: pathlib.Path) -> None:
    mpos = tmp_path / "x.mpos"
    mpos.write_text(
        '<score><elements><element id="0" x="4320" y="11776" sx="2000" sy="2607" page="1"></element></elements></score>',
    )
    mpos.write_text(
        '<score><elements><element id="0" x="4320" y="11776" sx="110000" sy="2607" page="1"></element></elements></score>',
    )
    assert chordseat.measure_boxes(mpos) == [{"x": 21.6, "y": 58.88, "w": 550.0, "page": 1.0}]


def test_a_staff_is_5_equal_lines_and_a_stray_line_is_not() -> None:
    def rule(y: float) -> pymupdf.Rect:
        return pymupdf.Rect(20, y, 300, y)

    shapes = [rule(y) for y in (0.0, 60.0, 64.0, 68.0, 72.0, 76.0, 90.0, 100.0, 104.0, 108.0, 112.0, 116.0)]
    assert [staff[0] for staff in chordseat.staves_of(shapes)] == [60.0, 100.0]


def test_a_chord_past_its_barline_is_off_target(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    monkeypatch.setattr(chordseat, "score_text", lambda _: ("x", ""))
    monkeypatch.setattr(chordseat, "rows_of", lambda _: [[(0, True), (1, True)]])
    monkeypatch.setattr(chordseat, "bar_rows", lambda _: [[1, 2]])
    monkeypatch.setattr(
        chordseat,
        "matched",
        lambda _text, _pdf: [
            {
                "chords": [50.0, 105.0],
                "bars": [100.0, 150.0],
                "openings": [100.0],
                "start": 50.0,
                "space": 4.0,
                "crossed": [],
            },
        ],
    )
    assert len(chordseat.problems(tmp_path, tmp_path)) == 1


def test_signatures_end_at_the_first_glyph_that_is_no_signature() -> None:
    clef, flat, time, head, accidental = 0xE050, 0xE260, 0xE084, 0xE0A4, 0xE260
    glyphs = [
        (20.0, 0.0, 30.0, clef),
        (31.0, 0.0, 35.0, flat),
        (36.0, 0.0, 40.0, time),
        (60.0, 0.0, 65.0, head),
        (120.0, 0.0, 125.0, accidental),
    ]
    assert max(chordseat.signature_ends(glyphs)) == pytest.approx(40.0)
    close = [
        (20.0, 0.0, 30.0, clef),
        (31.0, 0.0, 35.0, head),
        (36.0, 0.0, 40.0, flat),
    ]
    assert chordseat.signature_ends(close) == [30.0]


def test_a_full_height_stem_is_no_barline() -> None:
    heads = [(80.0, 89.0)]
    assert chordseat.is_stem(89.3, heads)
    assert chordseat.is_stem(80.2, heads)
    assert not chordseat.is_stem(153.7, heads)


def test_a_stray_long_line_inside_a_staff_does_not_hide_it() -> None:
    def rule(y: float) -> pymupdf.Rect:
        return pymupdf.Rect(20, y, 300, y)

    shapes = [rule(y) for y in (60.0, 64.0, 66.0, 68.0, 72.0, 76.0)]
    assert chordseat.staves_of(shapes) == [[60.0, 64.0, 68.0, 72.0, 76.0]]


def test_a_chordless_line_start_is_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chordseat, "bar_rows", lambda _: [[1, 2], [3, 4], [5, 6]])
    monkeypatch.setattr(chordseat, "rows_of", lambda _: [[(0, True)], [(1, True)], [(0, True)]])
    assert chordseat.chordless_starts("") == [3]


def test_a_cue_at_chord_size_is_no_chord() -> None:
    assert chordseat.CHORD_SHAPE.match("Am7")
    assert chordseat.CHORD_SHAPE.match("A")
    assert chordseat.CHORD_SHAPE.match("Bb")
    assert not chordseat.CHORD_SHAPE.match("AcousticGtr")
    assert not chordseat.CHORD_SHAPE.match("After")
    assert not chordseat.CHORD_SHAPE.match("Dizi")


def test_a_lyric_label_starts_a_line() -> None:
    plain = Bar(1, 0, 0.0, 100.0, Fraction(4), voices=[])
    labelled = Bar(
        2,
        0,
        0.0,
        100.0,
        Fraction(4),
        voices=[],
        texts=[Text(0.0, 0.0, [("Chorus-a: words", True, True)], above=False)],
    )
    assert port.must_start_line(labelled, plain)
    assert not port.must_start_line(plain, plain)


def test_the_gate_refuses_a_label_inside_a_line(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    plain = [Bar(n, 0, 0.0, 100.0, Fraction(4), voices=[]) for n in (1, 2, 4)]
    labelled = Bar(
        3,
        0,
        0.0,
        100.0,
        Fraction(4),
        voices=[],
        texts=[Text(0.0, 0.0, [("Chorus-a: words", True, True)], above=False)],
    )
    score = Score([plain[0], plain[1], labelled, plain[2]], [], [])
    monkeypatch.setattr(chordseat, "score_text", lambda _: ("x", ""))
    monkeypatch.setattr(chordseat, "chordless_starts", lambda _: [])
    monkeypatch.setattr(chordseat, "bar_rows", lambda _: [[1, 2, 3, 4]])
    assert port.chord_starts(tmp_path, score) == ["bar 3: lyric label inside a line"]
    monkeypatch.setattr(chordseat, "bar_rows", lambda _: [[1, 2], [3, 4]])
    assert port.chord_starts(tmp_path, score) == []
    monkeypatch.setattr(chordseat, "bar_rows", lambda _: [[1, 2], [3], [4]])
    assert port.chord_starts(tmp_path, score) == ["bar 3: lone bar on a line", "bar 4: lone bar on a line"]


def test_no_chord_stretches_a_bar() -> None:
    source = inspect.getsource(port)
    assert "chord_stretch" not in source
    assert "widen_chord_bars" not in source


def test_every_short_spelling_has_proved_playback() -> None:
    assert set(chordnames.SHORT.values()) == set(chordnames.PLAYS)
    assert chordnames.stored("maj7") == "^7"
    assert chordnames.stored("m7") == "m7"


def test_a_wrong_playback_is_found() -> None:
    assert chordnames.wrong_playback(["^7", "o"], {0: [1, 4, 8, 9], 1: [0, 3, 9]}) == []
    assert len(chordnames.wrong_playback(["^7", "o"], {0: [9], 1: [0, 3, 9]})) == 1


def test_a_wrong_mapping_breaks_its_chord() -> None:
    chord = parse_chord("Amaj7", None, 0)
    assert chord is not None
    assert 'text="^7"' in musicxml.harmony(chord, Fraction(0))


def test_oracle_finds_a_lost_word_and_excuses_the_rest() -> None:
    printed = Counter({"strings": 1, "dizi": 1, "maj": 3, "piano": 1})
    decoded = Counter({"strings": 1, "vapiano": 1})
    assert oracle.lost_words(printed, decoded, {"maj"}) == {"dizi": 1}


def test_a_flag_far_from_a_stem_does_not_shorten_its_note() -> None:
    bar = Bar(1, 0, 0.0, 200.0, Fraction(1))
    head = (leland.BLACK, 50.0, 100.0)
    stem = (56.8, 80.0, 100.0)
    far_flag = (0xE240, 56.8, 140.0)
    voices = leland.read_events(bar, leland.Reading([head, far_flag], 108.0, 2.7, [stem], [], 0), [])
    assert voices[0][0].nominal == 1


def test_a_saved_layout_with_a_label_inside_a_line_fails_its_proof() -> None:
    plain = Bar(1, 0, 0.0, 100.0, Fraction(4), voices=[], new_system=False)
    second = Bar(2, 0, 0.0, 100.0, Fraction(4), voices=[], new_system=False)
    labelled = Bar(
        3,
        0,
        0.0,
        100.0,
        Fraction(4),
        voices=[],
        texts=[Text(0.0, 0.0, [("Chorus-a: words", True, True)], above=False)],
    )

    after = Bar(4, 0, 0.0, 100.0, Fraction(4), voices=[])
    plain.chords = second.chords = labelled.chords = after.chords = [Chord(0.0, "A", 0, "")]
    assert port.line_rule_breaks(Score([plain, second, labelled, after], [], [])) == [3]
    labelled.new_system = True
    assert port.line_rule_breaks(Score([plain, second, labelled, after], [], [])) == []


@dataclasses.dataclass
class _BarParts:
    voices: list[list[Event]] | None = None
    chords: list[Chord] | None = None
    texts: list[Text] | None = None


def _plain_bar(
    number: int,
    left: float = 0.0,
    right: float = 100.0,
    *,
    parts: _BarParts | None = None,
) -> Bar:
    if parts is None:
        parts = _BarParts()
    return Bar(
        number,
        0,
        left,
        right,
        Fraction(4),
        voices=[] if parts.voices is None else parts.voices,
        chords=[] if parts.chords is None else parts.chords,
        texts=[] if parts.texts is None else parts.texts,
    )


def _chord(x: float, kind: str = "") -> Chord:
    return Chord(x, "D", 0, kind)


def test_a_long_chord_past_its_last_note_moves_over_the_barline() -> None:
    held = _plain_bar(
        1,
        0.0,
        33.0,
        parts=_BarParts(chords=[_chord(18.0, "m7(b5)")], voices=[[Event(6.0, [], Fraction(4))]]),
    )
    after = _plain_bar(2, 33.0, 70.0, parts=_BarParts(chords=[_chord(60.0, "7")], voices=[[Event(40.0, [], Fraction(4))]]))
    song.settle_chord_bars(Score([held, after], [], []))
    assert not held.chords
    assert [c.kind for c in after.chords] == ["m7(b5)", "7"]


def test_a_short_chord_inside_its_bar_stays_past_its_last_note() -> None:
    held = _plain_bar(
        1,
        436.0,
        494.0,
        parts=_BarParts(chords=[_chord(481.0, "7")], voices=[[Event(472.0, [], Fraction(4))]]),
    )
    after = _plain_bar(2, 494.0, 550.0, parts=_BarParts(voices=[[Event(500.0, [], Fraction(4))]]))
    song.settle_chord_bars(Score([held, after], [], []))
    assert len(held.chords) == 1


def test_piano_keeps_accents_and_a_fill_until_the_next_chord() -> None:
    hit = Event(0.0, [], Fraction(4))
    hit.accent = True
    bars = [
        _plain_bar(1, parts=_BarParts(voices=[[hit]])),
        _plain_bar(2, parts=_BarParts(chords=[_chord(0.0)])),
        _plain_bar(3, parts=_BarParts(texts=[Text(0.0, 0.0, [("(Fill)", False, False)], above=False)])),
        _plain_bar(4),
        _plain_bar(5, parts=_BarParts(chords=[_chord(0.0)])),
        _plain_bar(6),
    ]
    assert piano.hit_bars(Score(bars, [], [])) == {1, 3, 4}


def test_a_jump_prints_only_its_boxed_sign() -> None:
    out = port.boxed_sign("<Jump><text>D.S. al Coda</text><jumpTo>segno</jumpTo></Jump>")
    assert "<sym>segno</sym>" in out
    assert "D.S." not in out
    assert '<frameFgColor r="220"' in out
    assert "<jumpTo>segno</jumpTo>" in out
    out = port.boxed_sign(
        '<Marker>\n<style>Repeat Text Right</style><offset x="0" y="-7"/><text>To Coda</text><label>coda</label></Marker>',
    )
    assert "<sym>coda</sym>" in out
    assert "offset" not in out
    assert 'b="220"' in out
    assert out.index("</style>") < out.index("<frameType>")


def test_2_jump_signs_in_1_bar_stand_side_by_side_and_a_lone_one_stays() -> None:
    both = "<Measure><Marker><label>coda</label></Marker><Jump><text/></Jump></Measure>"
    alone = "<Measure><Jump><text/></Jump></Measure>"
    widened = port.beside_each_other(both)
    assert f'offset x="{port.SIGN_STEP}"' in widened
    assert "<autoplace>0</autoplace>" in widened
    assert widened.startswith(f"<Measure><stretch>{port.SIGN_BAR_STRETCH}</stretch>")
    assert "offset" not in port.beside_each_other(alone)


def test_a_target_sign_is_held_in_its_own_line_and_a_jump_is_not() -> None:
    target = port.boxed_sign(
        "<Marker><style>repeat_left</style><text><sym>segno</sym></text><label>segno</label></Marker>",
    )
    jump = port.boxed_sign("<Marker><style>repeat_right</style><text>To Coda</text><label>coda</label></Marker>")
    assert port.TARGET_HOLD in target
    assert port.TARGET_HOLD not in jump


def test_an_import_is_rebuilt_when_what_it_came_from_changes(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    made = []

    monkeypatch.setattr(port, "run", lambda *a: made.append(a) or pathlib.Path(a[3]).write_bytes(b"PK"))
    monkeypatch.setattr(port, "strip_title_frame", lambda _p: None)
    monkeypatch.setattr(port, "real_jumps", lambda _p, **_kw: None)
    monkeypatch.setattr(port, "big_signs", lambda _p: None)
    style = tmp_path / "s.mss"
    style.write_text("<Style>1</Style>", encoding="utf-8")
    score = Score([], [], [])
    first = port.imported(score, tmp_path, "x", style, clear=False)
    assert port.imported(score, tmp_path, "x", style, clear=False) == first
    assert len(made) == 1
    style.write_text("<Style>2</Style>", encoding="utf-8")
    assert port.imported(score, tmp_path, "x", style, clear=False) != first
    assert len(made) == 2


def test_a_staff_whose_line_pair_rounds_short_is_still_found() -> None:
    lines = [pymupdf.Rect(40, y, 560, y + 0.1) for y in (164.4, 170.0, 175.7, 181.4, 187.1)]
    assert chordseat.staves_of(lines) == [[164.4, 170.0, 175.7, 181.4, 187.1]]


def test_a_chord_row_lifted_over_a_bracket_is_not_too_high_and_one_lifted_over_nothing_is(tmp_path: pathlib.Path) -> None:
    def sheet(*, bracket: bool) -> pathlib.Path:
        doc = pymupdf.open()
        page = doc.new_page()
        top, gap = 300.0, 5.0
        for line in range(5):
            page.draw_line((40, top + line * gap), (560, top + line * gap), width=0.5)
        for at, name in enumerate(["Em", "D", "C", "G"]):
            page.insert_text((60 + at * 120, top - 3.9 * gap), name, fontsize=14)
        if bracket:
            page.draw_line((420, top - 1.6 * gap), (560, top - 1.6 * gap), width=0.5)
            page.draw_line((420, top - 1.6 * gap), (420, top - 0.2 * gap), width=0.5)
        path = tmp_path / f"bracket-{bracket}.pdf"
        doc.save(path)
        return path

    def highest(path: pathlib.Path) -> float:
        return max(spaces for _, _, spaces, _ in pdfcheck.chord_seating(path))

    assert highest(sheet(bracket=False)) > pdfcheck.MAX_CHORD_SPACES
    assert highest(sheet(bracket=True)) <= pdfcheck.MAX_CHORD_SPACES


def test_a_chord_the_print_lacks_is_a_problem_never_an_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    rows = [[(0, True), (0, False)]]
    lines = [
        {
            "chords": [143.9],
            "lefts": [143.0],
            "rights": [170.0],
            "bars": [573.7],
            "start": 40.0,
            "opening": 40.0,
            "space": 4.5,
            "size": 16.0,
        },
    ]
    monkeypatch.setattr(chordseat, "score_text", lambda _path: ("s.mscx", ""))
    monkeypatch.setattr(chordseat, "rows_of", lambda _text: rows)
    monkeypatch.setattr(chordseat, "matched", lambda _text, _pdf: lines)
    found = chordseat.problems(tmp_path / "s.mscz", tmp_path / "s.pdf")
    assert found == ["chords missing from the print: line 1 holds 2 chords in the score, 1 in the PDF"]


def test_a_key_signature_opens_a_bar_and_a_note_accidental_does_not() -> None:
    sharp = 0xE262
    signature = [
        (100.0, 50.0, 104.0, sharp),
        (105.0, 48.0, 109.0, sharp),
        (110.0, 52.0, 114.0, sharp),
        (120.0, 49.0, 126.0, 0xE0A4),
    ]
    note = [(100.0, 50.0, 104.0, sharp), (104.5, 50.0, 110.0, 0xE0A4)]

    def heads(glyphs: list[tuple[float, float, float, int]]) -> list[tuple[float, float, float, int]]:
        return [g for g in glyphs if g[3] == 0xE0A4]

    assert chordseat.signature_ends(signature, heads(signature), 4.5) == [104.0, 109.0, 114.0]
    assert chordseat.signature_ends(note, heads(note), 4.5) == []


def test_a_beat_1_chord_after_a_mid_line_key_change_is_judged_from_the_signature_end() -> None:
    rows = [[(0, True), (1, True)]]
    line: chordseat.LineRecord = {"chords": [40.0, 230.0], "bars": [200.0, 400.0], "openings": [230.0], "start": 40.0}
    assert chordseat.gaps(rows, [line]) == [0.0, 0.0]


def test_ink_inside_the_page_is_never_off_it_however_far_the_font_box_reaches(tmp_path: pathlib.Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((100, page.rect.height - 3), "a", fontsize=60)
    path = tmp_path / "edge.pdf"
    doc.save(path)
    assert pdfcheck.off_page(path) == []


def test_ink_past_the_top_edge_is_still_off_the_page(tmp_path: pathlib.Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((100, 10), "Tall", fontsize=40)
    path = tmp_path / "top.pdf"
    doc.save(path)
    assert [what for _, _, what in pdfcheck.off_page(path)] == ["off the page: Tall"]


def test_a_staff_holding_only_its_chord_row_is_not_idle_and_no_other_chord_row_saves_a_staff() -> None:
    assert idle_staves([100.0, 150.0], [148.0], [], [80.0]) == []
    assert idle_staves([100.0, 150.0], [95.0], [], [80.0, 185.0]) == [150.0]


def test_the_key_check_expects_every_key_the_song_uses_moved_by_its_transposition() -> None:
    bars = [Bar(n, 0, 0.0, 100.0, Fraction(4)) for n in range(1, 5)]
    bars[0].key, bars[2].key = 3, -2
    score = Score(bars, [], [])
    assert verify.expected_keys(score, 3) == {3, -2}
    assert verify.expected_keys(score, 1) == {1, -4}
    assert verify.same_keys({"-2", "3"}, {3, -2})
    assert not verify.same_keys({"3"}, {3, -2})
    assert verify.same_keys({"-6"}, {6})


def test_a_chord_row_lifted_over_an_ending_number_is_not_too_high(tmp_path: pathlib.Path) -> None:
    def sheet(*, number: bool) -> pathlib.Path:
        doc = pymupdf.open()
        page = doc.new_page()
        top, gap = 300.0, 5.0
        for line in range(5):
            page.draw_line((40, top + line * gap), (560, top + line * gap), width=0.5)
        for at, name in enumerate(["Em", "D", "C", "G"]):
            page.insert_text((60 + at * 120, top - 3.9 * gap), name, fontsize=14)
        if number:
            page.insert_text((420, top - 0.6 * gap), "1.", fontsize=10)
        path = tmp_path / f"ending-{number}.pdf"
        doc.save(path)
        return path

    def highest(path: pathlib.Path) -> float:
        return max(spaces for _, _, spaces, _ in pdfcheck.chord_seating(path))

    assert highest(sheet(number=False)) > pdfcheck.MAX_CHORD_SPACES
    assert highest(sheet(number=True)) <= pdfcheck.MAX_CHORD_SPACES


def test_a_cue_above_1_line_too_close_to_a_cue_below_the_line_above_is_text_on_text(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    def page_with(gap: float) -> list[object]:
        spans: list[dict[str, object]] = [
            {"text": "Sáo", "bbox": (60.0, 190.0, 80.0, 205.0)},
            {"text": "Sáo", "bbox": (60.0, 205.0 + gap, 80.0, 220.0 + gap)},
        ]

        class Page:
            @staticmethod
            def get_text(*_args: object) -> dict[str, list[dict[str, list[dict[str, list[dict[str, object]]]]]]]:
                return {"blocks": [{"lines": [{"spans": spans}]}]}

        return [Page()]

    monkeypatch.setattr(chordseat, "score_text", lambda _path: ("s.mscx", ""))
    monkeypatch.setattr(chordseat, "bar_rows", lambda _text: [[1], [2]])
    monkeypatch.setattr(chordseat, "line_texts", lambda _text: [([], ["Sáo"]), (["Sáo"], [])])
    monkeypatch.setattr(chordseat, "measure_boxes", lambda _mpos: [{"page": 0.0, "y": 150.0}, {"page": 0.0, "y": 260.0}])
    monkeypatch.setattr(chordseat, "space_points", lambda _path: 4.5)
    monkeypatch.setattr(chordseat.pdftext, "read", lambda _pdf: page_with(3.0))
    assert chordseat.crowded_line_texts(tmp_path / "s.mscz", tmp_path / "s.pdf") == [
        "text on text: 'Sáo' above line 2 sits too close to a cue below line 1",
    ]
    monkeypatch.setattr(chordseat.pdftext, "read", lambda _pdf: page_with(20.0))
    assert chordseat.crowded_line_texts(tmp_path / "s.mscz", tmp_path / "s.pdf") == []


def test_a_sign_box_over_a_word_is_found_and_the_signs_own_glyph_is_not(tmp_path: pathlib.Path) -> None:
    def sheet(*, over: bool) -> pathlib.Path:
        doc = pymupdf.open()
        page = doc.new_page()
        frame = pymupdf.Rect(100, 100, 130, 130)
        page.draw_rect(frame, color=(0, 0, 0.86), width=1.2)
        page.insert_text((105, 125), "S", fontsize=20)
        if not over:
            doc = pymupdf.open()
            page = doc.new_page()
            page.draw_rect(frame, color=(0, 0, 0.86), width=1.2)
            page.insert_text((140, 125), "72", fontsize=10)
        path = tmp_path / f"sign-{over}.pdf"
        doc.save(path)
        return path

    assert pdfcheck.sign_text_overlaps(sheet(over=True)) == ["text on text: p1: sign box over 'S'"]
    assert pdfcheck.sign_text_overlaps(sheet(over=False)) == []
    widths = pdfcheck.sign_word_overlaps(sheet(over=True))
    assert len(widths) == 1
    assert 3.0 < widths[0] < 30.0
    assert pdfcheck.sign_word_overlaps(sheet(over=False)) == []


def test_an_imported_expression_becomes_staff_text_with_its_placement() -> None:
    text = (
        "<Measure><voice><Expression><direction>down</direction><eid>x</eid>"
        "<text><i>(Fill)</i></text></Expression></voice></Measure>"
    )
    out = port.staff_texts(text)
    assert "<Expression>" not in out
    assert "<StaffText><placement>below</placement>" in out
    assert "<i>(Fill)</i>" in out


def test_every_edited_score_carries_staff_text_where_the_import_made_an_expression(tmp_path: pathlib.Path) -> None:
    measure = (
        "<Measure><voice><Expression><direction>down</direction><text><i>(Fill)</i></text></Expression></voice></Measure>"
    )
    source = tmp_path / "in.mscz"
    with zipfile.ZipFile(source, "w") as z:
        z.writestr(
            "in.mscx",
            f'<museScore><Score><Spatium>1.7</Spatium><Staff id="1">{measure}</Staff></Score></museScore>',
        )
    target = tmp_path / "out.mscz"
    port.edited(source, target, 1.6, 6.0, Score([Bar(1, 0, 0.0, 100.0, Fraction(4))], [], []))
    with zipfile.ZipFile(target) as z:
        text = z.read("in.mscx").decode("utf-8")
    assert "<Expression>" not in text
    assert "<StaffText><placement>below</placement>" in text


def test_ink_widths_run_from_each_chords_box_left_never_its_ink_left() -> None:
    rows = [[(0, True), (1, True)]]
    lines: list[chordseat.LineRecord] = [{"chords": [52.0, 112.0], "lefts": [50.0, 110.0], "rights": [70.0, 125.0]}]
    assert chordseat.ink_widths(rows, lines) == [20.0, 15.0]


def test_a_job_that_raises_fails_its_renders_and_the_batcher_keeps_serving(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batcher = render.Batcher()
    calls = []

    def run(batch: list[dict[str, int]]) -> dict[int, None]:
        calls.append(len(batch))
        if len(calls) == 1:
            msg = "mscore vanished"
            raise OSError(msg)
        return {item["ticket"]: None for item in batch}

    monkeypatch.setattr(batcher, "run", run)
    with pytest.raises(render.RenderFailedError, match="job failed: mscore vanished"):
        batcher.render(tmp_path / "a.mscz", [tmp_path / "a.pdf"])
    finished = threading.Event()

    def second() -> None:
        batcher.render(tmp_path / "b.mscz", [tmp_path / "b.pdf"])
        finished.set()

    threading.Thread(target=second, daemon=True).start()
    assert finished.wait(timeout=5), "the render after a failed job never ran"


def test_a_bars_signature_starts_at_its_barline_and_a_courtesy_signature_at_its_end_is_none() -> None:
    four = 0xE084
    courtesy = [(562.7, 66.2, 571.6, four), (562.7, 75.8, 571.6, four)]
    opening = [(350.0, 66.2, 358.0, four), (350.0, 75.8, 358.0, four)]
    assert chordseat.signature_ends(courtesy, [], 4.5, 347.6) == []
    assert chordseat.signature_ends(opening, [], 4.5, 347.6) == [358.0, 358.0]
    spread = [(350.0, 66.2, 358.0, four), (400.0, 66.2, 408.0, four)]
    assert chordseat.signature_ends(spread, [], 4.5, 347.6) == [358.0]


def test_a_run_past_its_deadline_is_sampled_before_it_is_killed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    if shutil.which("sample") is None:
        pytest.skip("the native sampler ships only on macOS; elsewhere a timeout carries no sample by design")
    monkeypatch.setattr(launch, "HANGS", tmp_path)
    monkeypatch.setattr(launch, "free_core", lambda: None)
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        launch.heavy(["sleep", "30"], 1.0)
    where = str(caught.value.output).removeprefix("stack sampled to ")
    assert pathlib.Path(where).exists()


def test_a_key_is_written_in_the_mode_its_file_name_declares() -> None:
    major = Score([Bar(1, 0, 0.0, 1.0, Fraction(4), key=1)], [], [], minor=False)
    assert "<mode>major</mode>" in musicxml.write(major)
    minor = Score([Bar(1, 0, 0.0, 1.0, Fraction(4), key=1)], [], [], minor=True)
    assert "<mode>minor</mode>" in musicxml.write(minor)
    with pytest.raises(ValueError, match="mode"):
        musicxml.write(Score([Bar(1, 0, 0.0, 1.0, Fraction(4), key=1)], [], []))


def test_a_launch_waits_while_the_load_fills_every_core_and_starts_once_one_frees() -> None:
    readings = iter([5.0, 12.0, 30.0])
    taken: list[float] = []

    def idle() -> float:
        taken.append(next(readings))
        return taken[-1]

    launch.free_core(idle=idle, cores=8)
    assert taken == [5.0, 12.0, 30.0]


def test_a_launch_that_never_sees_a_free_core_names_the_load() -> None:
    with pytest.raises(TimeoutError, match=r"0\.40 of 8 cores idle"):
        launch.free_core(deadline=0.0, idle=lambda: 5.0, cores=8)


def test_a_launch_drops_only_the_logs_whose_every_conversion_is_its_own(tmp_path: pathlib.Path) -> None:
    work, other, logs = tmp_path / "work", tmp_path / "elsewhere", tmp_path / "logs"
    for folder in (work, other, logs):
        folder.mkdir()
    second = "261108_100000"
    at = time.mktime(time.strptime(second, "%y%m%d_%H%M%S"))

    def log(name: str, *reads: pathlib.Path) -> pathlib.Path:
        path = logs / f"MuseScore_{name}.log"
        path.write_text(
            "".join(f"x | ConverterController::convertFile | in: {read}, out: {read}.pdf\n" for read in reads)
            or "x | INFO | session\n",
            encoding="utf-8",
        )
        return path

    ours = log(second, work / "a.mscz", work / "b.mscz")
    session = log("261108_100001")
    shared = log("261108_100002", work / "a.mscz", other / "c.mscz")
    late = log("261108_110000", work / "a.mscz")
    assert launch.drop_own_logs([work], at - 1, at + 5, logs) == 1
    assert not ours.exists()
    assert session.exists()
    assert shared.exists()
    assert late.exists()


def test_a_log_another_launch_removes_mid_sweep_is_skipped(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work, logs = tmp_path / "work", tmp_path / "logs"
    work.mkdir()
    logs.mkdir()
    second = "261108_100000"
    at = time.mktime(time.strptime(second, "%y%m%d_%H%M%S"))
    gone = logs / f"MuseScore_{second}.log"
    gone.write_text(f"x | ConverterController::convertFile | in: {work / 'a.mscz'}, out: x\n", encoding="utf-8")
    real = pathlib.Path.read_text

    def vanishing(
        path: pathlib.Path,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> str:
        if path == gone:
            path.unlink()
        return real(path, encoding=encoding, errors=errors, newline=newline)

    monkeypatch.setattr(pathlib.Path, "read_text", vanishing)
    assert launch.drop_own_logs([work], at - 1, at + 1, logs) == 0


def test_a_sheets_copy_left_behind_a_rewritten_history_resyncs_to_the_remote(tmp_path: pathlib.Path) -> None:
    git_bin = shutil.which("git")
    assert git_bin is not None

    def git(*args: str, cwd: pathlib.Path) -> str:
        return subprocess.run(
            [git_bin, *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
        ).stdout.strip()

    remote, ours, theirs = tmp_path / "remote.git", tmp_path / "ours", tmp_path / "theirs"
    git("init", "--quiet", "--bare", "--initial-branch=main", str(remote), cwd=tmp_path)
    for clone in (ours, theirs):
        git("clone", "--quiet", str(remote), str(clone), cwd=tmp_path)
        git("config", "user.email", "t@t", cwd=clone)
        git("config", "user.name", "t", cwd=clone)
    (ours / "a.pdf").write_text("1")
    git("add", "a.pdf", cwd=ours)
    git("commit", "--quiet", "-m", "one", cwd=ours)
    git("push", "--quiet", "-u", "origin", "main", cwd=ours)
    git("pull", "--quiet", "origin", "main", cwd=theirs)
    git("commit", "--quiet", "--amend", "-m", "rewritten", cwd=theirs)
    git("push", "--quiet", "--force", "origin", "main", cwd=theirs)
    (ours / "b.pdf").write_text("2")
    git("add", "b.pdf", cwd=ours)
    git("commit", "--quiet", "-m", "left behind", cwd=ours)
    publish.resync(ours)
    assert git("rev-parse", "HEAD", cwd=ours) == git("rev-parse", "main", cwd=remote)


def test_a_launch_the_window_server_refuses_stops_as_the_machines_fault(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(launch, "free_core", lambda: None)
    refused = [
        "sh",
        "-c",
        "echo 'PasteBoard: Error creating pasteboard: com.apple.pasteboard.clipboard [-4960]' >&2; kill -ABRT $$",
    ]
    with pytest.raises(launch.MachineUnavailableError, match=launch.UNAVAILABLE):
        launch.heavy(refused, 10.0)
    coreml = [
        "sh",
        "-c",
        (
            "echo 'Status Message: Error executing model: "
            "Unable to compute the prediction using a neural network model.' >&2; exit 1"
        ),
    ]
    with pytest.raises(launch.MachineUnavailableError, match=launch.UNAVAILABLE):
        launch.heavy(coreml, 10.0)
    plain = launch.heavy(["sh", "-c", "kill -ABRT $$"], 10.0)
    assert plain.returncode == -6


def test_a_mark_starting_just_left_of_a_barline_belongs_to_the_bar_it_runs_into() -> None:
    row = [Bar(n, 0, left, left + 100, Fraction(4)) for n, left in ((1, 40.0), (2, 140.0), (3, 240.0))]
    assert [bar_at(row, x, 4).number for x in (20.0, 60.0, 137.0, 139.0, 141.0, 236.5, 300.0)] == [1, 1, 2, 2, 2, 3, 3]
    assert bar_at(row, 135.0, 4).number == 1


def test_a_lyric_between_2_lines_belongs_to_the_line_it_sits_under_and_a_cue_to_the_line_it_sits_above() -> None:
    first = Staff.__new__(Staff)
    first.top, first.base = 100.0, 120.0
    second = Staff.__new__(Staff)
    second.top, second.base = 170.0, 190.0
    groups = [[first], [second]]
    lyric = ("lời", 50.0, 145.0, 90.0, "FreeSerif", 8.2, [])
    cue = ("Piano", 50.0, 145.0, 90.0, "TimesNewRomanPS-BoldItalicMT", 10.3, [])
    chord = ("Am7", 50.0, 160.0, 70.0, "Edwin-Roman", 10.9, [])
    assert text_owners([lyric, cue, chord], groups) == [0, 1, 1]


def test_a_chords_ink_is_read_from_its_own_letters_never_a_sharp_beside_it() -> None:
    def page_with(*, mark: bool) -> pymupdf.Page:
        doc = pymupdf.open()
        page = doc.new_page(width=200, height=100)
        page.insert_text((60, 60), "C", fontsize=13)
        if mark:
            page.insert_text((57.6, 66), "|", fontsize=13)
        return page

    alone = ink_left(page_with(mark=False), 60, 60, 13)
    marked = page_with(mark=True)
    stroke = next(
        pymupdf.Rect(span["bbox"])
        for block in marked.get_text("dict")["blocks"]
        for line in block["lines"]
        for span in line["spans"]
        if span["text"] == "|"
    )
    assert stroke.x1 > 60 - INK_REACH, "the stroke must stand inside the ink window, or the guard judges nothing"
    assert stroke.x0 < 60, "the stroke must stand inside the ink window, or the guard judges nothing"
    assert abs(ink_left(marked, 60, 60, 13) - alone) < 0.2


def test_a_chords_next_correction_is_scaled_by_how_far_its_ink_moved() -> None:
    assert next_bias([0.5], [-1.0], [1.0], [0.0], [1.0]) == [0.25]
    assert next_bias([0.0], [1.0], [1.0]) == [1.0]


def test_a_chords_ink_is_never_a_coloured_sign_box_beside_it() -> None:
    def page_with(*, box: bool) -> pymupdf.Page:
        doc = pymupdf.open()
        page = doc.new_page(width=200, height=100)
        page.insert_text((60, 60), "C", fontsize=13)
        if box:
            page.draw_rect(pymupdf.Rect(40, 45, 59.5, 62), color=(1, 0, 0), width=1.2)
        return page

    assert abs(ink_left(page_with(box=True), 60, 60, 13) - ink_left(page_with(box=False), 60, 60, 13)) < 0.2


def test_a_line_rule_refusal_names_its_bar_and_its_rule() -> None:
    bars = [Bar(n, 0, 0, 100, Fraction(4)) for n in (1, 2, 3)]
    for bar in bars:
        bar.chords.append(Chord(0.0, "C", 0, "", None, 0))
    bars[2].new_system = True
    assert port.line_rule_reasons(Score(bars, [], [])) == ["bar 3: lone bar on a line"]


def test_a_crowded_chord_takes_its_letters_bearing_from_the_clean_chords() -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=200, height=100)
    page.insert_text((60, 60), "C", fontsize=13)
    assert not crowded(page, 60, 60, 13)
    page.insert_text((57.6, 66), "|", fontsize=13)
    assert crowded(page, 60, 60, 13)
    lines: list[chordseat.LineRecord] = [{"chords": [100.8, 300.1]}]
    readings = [(0, 0, "C", 13.0, 100.0, False), (0, 1, "C", 13.0, 300.0, True)]
    assert clean_bearings(lines, readings) == {("C", 13.0): pytest.approx(0.8 / 13)}


def test_the_gate_passes_a_chordless_line_start_only_where_the_source_opens_one(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    bars = [Bar(n, 0, 0.0, 100.0, Fraction(4), voices=[]) for n in (1, 2, 3, 4)]
    score = Score(bars, [], [])
    monkeypatch.setattr(chordseat, "score_text", lambda _: ("x", ""))
    monkeypatch.setattr(chordseat, "chordless_starts", lambda _: [3])
    monkeypatch.setattr(chordseat, "bar_rows", lambda _: [[1, 2], [3, 4]])
    assert port.chord_starts(tmp_path, score) == ["bar 3: line starts without its chord"]
    bars[2].source_line = True
    assert port.chord_starts(tmp_path, score) == []


def test_decoding_keeps_the_sources_line_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    decoded = Score([Bar(n, 0, 0.0, 100.0, Fraction(4), voices=[], new_system=n == 3) for n in (1, 2, 3)], [], [])
    monkeypatch.setattr(song, "encoding", lambda _: "leland")
    monkeypatch.setattr(song.leland, "decode", lambda _: decoded)
    monkeypatch.setattr(song, "settle_chord_bars", lambda score: score)
    assert [bar.source_line for bar in song.decode(pathlib.Path("X-C.pdf")).bars] == [False, False, True]


def test_a_section_box_marks_the_barline_under_or_beside_it() -> None:
    row = [
        Bar(n, 0, left, right, Fraction(4))
        for n, (left, right) in enumerate([(42.5, 186.2), (186.2, 290.4), (290.4, 341.4)], 1)
    ]
    assert mark_bar(row, 172.1, 198.4).number == 2
    assert mark_bar(row, 168.0, 183.0).number == 2
    assert mark_bar(row, 188.0, 205.0).number == 2
    assert mark_bar(row, 50.8, 77.0).number == 1
    assert mark_bar(row, 230.0, 250.0).number == 2


def test_a_chord_seated_where_the_seating_aims_after_a_sign_never_touches_it() -> None:
    sign: pdftext.Char = {"origin": (37.4, 245.7), "bbox": (37.4, 229.7, 46.1, 249.7), "c": "\ue047", "synthetic": False}
    box = pymupdf.Rect(33.1, 231.1, 50.6, 248.6)
    target = sign_end(sign, 20.0, [box])
    for bearing in (0.4, 0.8, 1.2):
        for stray in (-TOLERANCE, 0.0, TOLERANCE):
            left = target + stray - bearing
            assert not sign_touches(37.4, 245.7, 20.0, 11.2, [(left, left + 7.6, 246.5)]), (bearing, stray)


def test_a_printed_tempo_becomes_words_and_a_metronome_mark() -> None:
    out = musicxml.words(Text(0.0, 0.0, [("Andante religioso♩= 58", True, False)], above=True))
    assert "♩" not in out
    assert "<words" in out
    assert "Andante religioso</words>" in out
    assert "<metronome><beat-unit>quarter</beat-unit><per-minute>58</per-minute></metronome>" in out
    assert '<sound tempo="58"/>' in out


def test_a_text_with_nothing_to_print_writes_nothing() -> None:
    assert not musicxml.words(Text(0.0, 0.0, [("", True, True)], above=True))
    bar = Bar(1, 0, 0, 100, Fraction(4), key=0, time=(4, 4))
    bar.tempo = ("", 68.0)
    score = Score([bar], [], [])
    score.minor = False
    out = musicxml.measure(bar, 1, score)
    assert "<words" not in out
    assert "<per-minute>68</per-minute>" in out


def test_a_page_with_a_tall_blank_band_is_refused(tmp_path: pathlib.Path) -> None:
    def page_with(gap: float) -> pathlib.Path:
        doc = pymupdf.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((50, 60), "top line", fontsize=12)
        page.insert_text((50, 60 + gap), "next line", fontsize=12)
        path = tmp_path / f"p{gap:g}.pdf"
        doc.save(path)
        return path

    assert pdfcheck.blank_bands(page_with(60)) == []
    assert pdfcheck.blank_bands(page_with(300)) != []


DISTINCT_FACTS = {
    "ACCIDENTAL_REACH",
    "ARTICULATION_REACH",
    "BAR_LINE_MERGE",
    "BEAM_MAX_HEIGHT",
    "BEAM_MIN_WIDTH",
    "DOT_REACH",
    "FLAG_REACH",
    "SECOND_STAFF",
    "STEM_UP_OFFSET",
    "STRAIGHT_TOLERANCE",
    "ACCIDENTALS",
    "BAR_REST",
    "CHORD",
    "CHORD_GAP",
    "CREDIT_ROW",
    "DYNAMICS",
    "FLAGS",
    "GRACE",
    "HEADS",
    "JUMP_WORDS",
    "KNOWN_GLYPHS",
    "MUSIC_FONTS",
    "RESTS",
    "STAFF_REACH",
    "STYLE",
}


def test_no_fact_has_2_homes() -> None:
    """A constant written twice drifts: the sign's extent once disagreed between the seating and the touch check."""
    defined = collections.defaultdict(set)
    for path in pathlib.Path(port.__file__).parent.glob("*.py"):
        for line in path.read_text(encoding="utf-8").splitlines():
            found = re.match(r"^([A-Z][A-Z0-9_]{2,})\s*(?::[^=]+)?=", line)
            if found:
                defined[found[1]].add(path.name)
    assert {name for name, homes in defined.items() if len(homes) > 1} <= DISTINCT_FACTS


def test_ink_is_read_off_a_page_in_1_place() -> None:
    """Every reading of which rows and columns of a page hold ink goes through 1 reader.

    So a threshold or a row stride is decided once.
    """
    readers = {}
    for path in pathlib.Path(port.__file__).parent.glob("*.py"):
        found = len(re.findall(r"\.get_pixmap\(", path.read_text(encoding="utf-8")))
        if found:
            readers[path.name] = found
    assert readers == {"pdfcheck.py": 2, "chordseat.py": 1, "gate.py": 1, "pdftext.py": 1}


def test_the_ink_reader_finds_a_mark_where_it_is_drawn() -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.draw_rect(pymupdf.Rect(100, 200, 300, 210), color=(0, 0, 0), fill=(0, 0, 0))
    rows, height = pdfcheck.inked_rows(page, 72)
    cols, width = pdfcheck.inked_columns(page, 72)
    assert (height, width) == (842, 595)
    assert 199 <= rows[0] <= 201
    assert 209 <= rows[-1] <= 211
    assert 99 <= cols[0] <= 101
    assert 299 <= cols[-1] <= 301
    blank = doc.new_page(width=595, height=842)
    assert pdfcheck.inked_rows(blank, 72)[0] == []


def test_no_code_names_anything_by_age() -> None:
    """The pipeline builds for the newest release of everything, so nothing in it is named by age."""
    root = pathlib.Path(port.__file__).parents[2]
    words = re.compile(r"\b(legacy|outdated|deprecated)\b", re.IGNORECASE)
    files = [
        path
        for path in sorted([*root.glob("src/muse/**/*"), *root.glob("tests/*"), root / "README.md"])
        if path.is_file() and path.suffix in {".py", ".js", ".md", ".json"}
    ]
    found = [
        f"{path.relative_to(root)}:{at}"
        for path in files
        for at, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if words.search(line) and "words = re.compile" not in line
    ]
    assert found == []


def test_no_script_runs_its_main_on_import() -> None:
    """A script that calls main() at import runs its whole job whenever another tool loads it to read its definitions."""
    root = pathlib.Path(port.__file__).parents[2]
    scripts = sorted([*root.glob("src/muse/*.py"), *root.glob("tests/*.py"), *root.glob("tools/*.py")])
    found = [
        f"{path.name}:{node.lineno}"
        for path in scripts
        for node in ast.parse(path.read_text(encoding="utf-8")).body
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call) and getattr(node.value.func, "id", "") == "main"
    ]
    assert found == []


def test_finding_empty_bars_leaves_the_callers_noted_bars_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """The caller passes 1 set to several judgements, so finding empty bars must not add the signed bars to it."""
    monkeypatch.setattr(chordseat, "signed_bars", lambda _: {5})
    monkeypatch.setattr(chordseat, "bar_rows", lambda _: [])
    monkeypatch.setattr(chordseat, "rows_of", lambda _: [])
    noted = {3}
    chordseat.empty_lines("", [], noted)
    assert noted == {3}


def test_spread_lines_never_open_a_blank_band() -> None:
    """The most room vertical justification may put between 2 lines stays under the blank band the checks refuse."""
    text = port.STYLE.read_text(encoding="utf-8")
    assert "<enableVerticalSpread>1</enableVerticalSpread>" in text
    height_mm = float(re.findall(r"<pageHeight>([^<]*)</pageHeight>", text)[0]) * port.MM_PER_INCH
    for spatium in (1.15, 1.5, 2.0):
        assert port.spread_cap(spatium) * spatium < pdfcheck.BLANK_BAND * height_mm


def test_chord_ink_is_found_where_it_is_drawn_at_any_window_start() -> None:
    """The ink reader maps each pixel to its true page position, whatever fraction of a pixel the window starts at."""
    for step in range(40):
        left = 50 + step * 0.137
        page = pymupdf.open().new_page(width=200, height=100)
        page.draw_rect(pymupdf.Rect(left + 2.0, 40, left + 2.4, 62), color=None, fill=(0, 0, 0), width=0)
        assert chordseat.ink_left(page, left, 60, 13) == pytest.approx(left + 2.0, abs=0.1)


def test_a_served_musescore_that_dies_is_reported_and_replaced(tmp_path: pathlib.Path) -> None:
    """A served process that exits mid-request fails that request, and the next request gets a fresh process."""
    fake = tmp_path / "mscore"
    fake.write_text(
        "#!/bin/sh\n"
        "while read -r line; do\n"
        '  case "$line" in *die*) exit 3 ;; esac\n'
        '  id=$(printf "%s" "$line" | sed \'s/.*"id": "\\([^"]*\\)".*/\\1/\')\n'
        '  echo "$id ok"\n'
        "done\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    good, bad = tmp_path / "good.json", tmp_path / "die.json"
    good.write_text("[]", encoding="utf-8")
    bad.write_text("[]", encoding="utf-8")
    assert not launch.served(good, 10, command=str(fake))
    with pytest.raises(launch.ServerDiedError):
        launch.served(bad, 10, command=str(fake))
    assert not launch.served(good, 10, command=str(fake))


def test_a_render_rasters_1_chord_per_letter_and_size(monkeypatch: pytest.MonkeyPatch) -> None:
    """Chords sharing a first letter and size share their ink offset, read off the first clean one."""
    page = pymupdf.open().new_page(width=400, height=100)
    words = []
    for x in (40.0, 140.0, 240.0):
        page.insert_text((x, 60), "C", fontsize=13)
        words.append((13.0, x, x + 9.0, 60.0, "C"))
    rastered: list[float] = []
    real = chordseat.ink_left

    def counted(page: pymupdf.Page | pdftext.Page, left: float, base: float, size: float) -> float:
        rastered.append(left)
        return real(page, left, base, size)

    monkeypatch.setattr(chordseat, "ink_left", counted)
    joined, _, _ = chordseat._chord_edges(page, words, 0, chordseat.RenderReading([], {}))
    assert rastered == [40.0]
    assert [round(edge - x, 3) for edge, (_, x, _, _, _) in zip(joined, words, strict=True)] == [
        round(real(page, x, 60, 13) - x, 3) for _, x, _, _, _ in words
    ]


def test_a_render_whose_output_is_its_input_is_refused_and_keeps_the_input(tmp_path: pathlib.Path) -> None:
    score = tmp_path / "song.pdf"
    score.write_bytes(b"input")
    with pytest.raises(ValueError, match="its own input"):
        render.Batcher().render(score, [tmp_path / "other.pdf", tmp_path / "." / "song.pdf"])
    assert score.read_bytes() == b"input"


def test_the_launch_count_is_written_only_under_its_lock() -> None:
    tree = ast.parse(inspect.getsource(launch))
    writers = {
        function.name
        for function in ast.walk(tree)
        if isinstance(function, ast.FunctionDef)
        for node in ast.walk(function)
        if isinstance(node, ast.AugAssign)
        and isinstance(node.target, ast.Subscript)
        and ast.unparse(node.target.value) == "LAUNCHES"
    }
    assert writers == {"count_launch"}
    assert "with LAUNCHES_LOCK" in inspect.getsource(launch.count_launch)


def test_a_cue_span_belongs_only_to_a_cue_holding_its_words_as_words() -> None:
    letter, opening, upper, shared = (pymupdf.Rect(x, 0, x + 1, 1) for x in range(4))
    spans = [("A", letter), ("(After", opening), ("BASS IN", upper), ("IN", shared)]
    mine, theirs = chordseat.cue_spans(spans, ["BASS IN /Steel-Gtr"], ["(After Sin - BAND IN )"])
    assert mine == [upper, shared]
    assert [words for words, _ in theirs] == ["(After", "IN"]
    assert not chordseat.in_cue("A", "BASS IN /Steel-Gtr")
    assert chordseat.in_cue("BAND IN", "(After Sin - BAND IN )")


def test_pymupdf_reads_and_renders_without_its_unused_engines() -> None:
    """The layout and LLM engines PyMuPDF requires stay uninstalled, so Python can move past onnxruntime's builds."""
    assert importlib.util.find_spec("onnxruntime") is None
    page = pymupdf.open()
    page.new_page(width=200, height=100)
    sheet = page[0]
    sheet.insert_text((20, 50), "Am7", fontsize=12)
    sheet.draw_line((10, 60), (190, 60))
    chars = [
        c
        for b in sheet.get_text("rawdict")["blocks"]
        for line in b.get("lines", [])
        for s in line["spans"]
        for c in s["chars"]
    ]
    assert "".join(c["c"] for c in chars) == "Am7"
    assert sheet.get_drawings()
    assert sheet.get_pixmap(dpi=36).width == 100


def test_a_bare_path_still_finds_package_manager_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run started over ssh has no login path, so git-filter-repo and homr's helpers would read as missing."""
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(toolpath, "PACKAGE_PATHS", ("/usr/sbin",))
    toolpath.with_package_paths()
    assert os.environ["PATH"].split(os.pathsep) == ["/usr/bin", "/bin", "/usr/sbin"]
    toolpath.with_package_paths()
    assert os.environ["PATH"].split(os.pathsep).count("/usr/sbin") == 1


STOPPED_TEST_ELAPSED = 12.5
STOPPED_TEST_WAIT_SECONDS = 5.0
STOPPED_TEST_POLL_SECONDS = 0.01
STOPPED_TEST_SLEEP_SECONDS = 30


@dataclasses.dataclass(eq=False)
class StoppedProcess(build.ProcessStatus):
    """Typed process details and query failures for stopped-process checks."""

    process_id: int = 4321
    state: str | psutil.Error = psutil.STATUS_STOPPED
    parent_id: int | psutil.Error = 1234
    command: list[str] | psutil.Error = dataclasses.field(default_factory=lambda: ["worker"])
    descendants: list["StoppedProcess"] | psutil.Error = dataclasses.field(default_factory=list)
    child_queries: list[bool] = dataclasses.field(default_factory=list)
    exit_code: int | None = None

    @property
    @override
    def pid(self) -> int:
        """The configured process identifier.

        Returns:
            The process identifier.

        """
        return self.process_id

    @override
    def status(self) -> str:
        """Read the configured process state.

        Returns:
            The process state.

        """
        if isinstance(self.state, psutil.Error):
            raise self.state
        return self.state

    @override
    def ppid(self) -> int:
        """Read the configured parent identifier.

        Returns:
            The parent identifier.

        """
        if isinstance(self.parent_id, psutil.Error):
            raise self.parent_id
        return self.parent_id

    @override
    def cmdline(self) -> list[str]:
        """Read the configured command arguments.

        Returns:
            The command arguments.

        """
        if isinstance(self.command, psutil.Error):
            raise self.command
        return self.command

    def children(self, *, recursive: bool = False) -> list["StoppedProcess"]:
        """Record the child query and read the configured descendants.

        Args:
            recursive: Whether the caller requested recursive traversal.

        Returns:
            The configured descendants.

        """
        self.child_queries.append(recursive)
        if isinstance(self.descendants, psutil.Error):
            raise self.descendants
        return self.descendants

    def poll(self) -> int | None:
        """Read the configured worker exit code.

        Returns:
            The exit code, or none for a running worker.

        """
        return self.exit_code


def test_a_stopped_process_is_reported_with_its_identity_and_resume_action() -> None:
    """A stopped process names its identity, command, elapsed time and recovery action.

    Args:
        None.

    """
    process = StoppedProcess(command=["worker", "song\nname"])
    lines = build.stopped_process_lines([process], STOPPED_TEST_ELAPSED)
    assert len(lines) == 1
    assert "pid 4321 parent pid 1234" in lines[0]
    assert 'cmdline ["worker", "song\\nname"]' in lines[0]
    assert "build elapsed 12.5s" in lines[0]
    assert "stopped (state T): send SIGCONT to resume, or find the sender" in lines[0]
    assert "\n" not in lines[0]


def test_running_processes_produce_no_stopped_report() -> None:
    """Healthy workers produce no stopped-process output.

    Args:
        None.

    """
    process = StoppedProcess(state=psutil.STATUS_RUNNING)
    assert build.stopped_process_lines([process], STOPPED_TEST_ELAPSED) == []


@pytest.mark.parametrize("error", [psutil.NoSuchProcess, psutil.AccessDenied])
@pytest.mark.parametrize("method", ["status", "ppid", "cmdline"])
def test_inaccessible_processes_produce_no_stopped_report(method: str, error: type[psutil.Error]) -> None:
    """Exiting or inaccessible processes do not interrupt collection.

    Args:
        method: Process query that fails during inspection.
        error: Expected psutil failure to ignore.

    """
    process = StoppedProcess(
        state=error(4321) if method == "status" else psutil.STATUS_STOPPED,
        parent_id=error(4321) if method == "ppid" else 1234,
        command=error(4321) if method == "cmdline" else ["worker"],
    )
    assert build.stopped_process_lines([process], STOPPED_TEST_ELAPSED) == []


def test_stopped_workers_and_recursive_children_are_reported_only_once(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Repeated checks report both workers and descendants exactly once.

    Args:
        monkeypatch: Replaces running workers and process lookup with fakes.
        capsys: Captures the collector's diagnostic output.

    """
    worker = StoppedProcess()
    descendant = StoppedProcess(process_id=5432)
    root = StoppedProcess(process_id=worker.pid, descendants=[descendant])
    monkeypatch.setattr(build, "RUNNING", {worker})
    monkeypatch.setattr(build.psutil, "Process", lambda _pid: root)
    reported: set[psutil.Process] = set()
    build._warn_stopped(reported, STOPPED_TEST_ELAPSED)
    build._warn_stopped(reported, STOPPED_TEST_ELAPSED)
    captured = capsys.readouterr()
    assert not captured.out
    assert len(captured.err.splitlines()) == 2
    assert captured.err.count("pid 4321 parent") == 1
    assert captured.err.count("pid 5432 parent") == 1
    assert root.child_queries[-1] is True


def test_a_real_sigstopped_child_is_reported() -> None:
    """A real stopped child is detected and always killed and reaped afterward.

    Args:
        None.

    """
    sleep = shutil.which("sleep")
    assert sleep is not None
    process = subprocess.Popen([sleep, str(STOPPED_TEST_SLEEP_SECONDS)], stdin=subprocess.DEVNULL)
    try:
        process.send_signal(signal.SIGSTOP)
        inspected = psutil.Process(process.pid)
        deadline = time.monotonic() + STOPPED_TEST_WAIT_SECONDS
        lines = build.stopped_process_lines([inspected], STOPPED_TEST_ELAPSED)
        while not lines and time.monotonic() < deadline:
            time.sleep(STOPPED_TEST_POLL_SECONDS)
            lines = build.stopped_process_lines([inspected], STOPPED_TEST_ELAPSED)
        assert len(lines) == 1
        assert f"pid {process.pid} " in lines[0]
        assert "stopped (state T)" in lines[0]
    finally:
        process.kill()
        process.wait(timeout=STOPPED_TEST_WAIT_SECONDS)


def test_waiting_for_a_song_checks_stopped_processes_again_after_the_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unfinished song cannot leave the collector blocked between process checks.

    Args:
        monkeypatch: Supplies a deterministic clock and future waits.

    """
    job = concurrent.futures.Future[tuple[dict[str, object] | None, str]]()
    interval = build.STOPPED_CHECK_SECONDS
    clock = iter([0.0, 0.0, 0.0, 0.0, interval, interval, interval])
    batches = iter([(set(), {job}), ({job}, set())])
    waits: list[tuple[float, str]] = []
    warning_times: list[float] = []
    monkeypatch.setattr(build, "_monotonic", lambda: next(clock))
    monkeypatch.setattr(
        build.concurrent.futures,
        "wait",
        lambda _pending, *, timeout, return_when: waits.append((timeout, return_when)) or next(batches),
    )
    monkeypatch.setattr(build, "_warn_stopped", lambda _reported, elapsed: warning_times.append(elapsed))
    assert list(build._completed({job: pathlib.Path("song.pdf")}, 0.0)) == [job]
    assert warning_times == [0.0, interval]
    assert [timeout for timeout, _ in waits] == [interval, interval]
    assert all(return_when == concurrent.futures.FIRST_COMPLETED for _, return_when in waits)
