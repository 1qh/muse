"""The piano sheet, chord timing, song folders and what a saved score holds."""

import zipfile
from fractions import Fraction
from pathlib import Path
from typing import TypedDict, Unpack

import pytest

from muse import gate, musicxml, piano, port, scorefile, verify
from muse.model import Bar, Chord, Event, Head, Score, Text
from muse.song import Song


class BarExtras(TypedDict, total=False):
    """Name the bar fields a test may set."""

    rehearsal: list[str]
    texts: list[Text]
    texts2: list[Text]
    voices2: list[list[Event]]
    chords: list[Chord]
    time: tuple[int, int]


def note(x: float = 1.0, length: Fraction = Fraction(4)) -> Event:
    return Event(x, [Head(x, 0.0, "C", 5)], length)


def bar(number: int, **kw: Unpack[BarExtras]) -> Bar:
    return Bar(number, 0, 0.0, 100.0, Fraction(4), voices=[[note()]], **kw)


def rest(number: int, **kw: Unpack[BarExtras]) -> Bar:
    return Bar(
        number, 0, 0.0, 100.0, Fraction(4), voices=[[Event(0.0, [], Fraction(4), bar_rest=True)]], **kw
    )


def lyric(words: str) -> Text:
    label, rest_of = words.split(":")
    return Text(0.0, 0.0, [(label + ": ", True, True), (rest_of, False, True)], above=False)


def song_score() -> Score:
    bars = [
        bar(1),
        bar(2),
        rest(3, rehearsal=["A"], texts=[lyric("Verse1: one two three four five six")]),
        bar(4),
        bar(5, rehearsal=["Interlude"]),
        bar(6),
        bar(7, rehearsal=["B"], texts=[lyric("Chorus: la la la la la la")]),
        bar(8),
        bar(9, rehearsal=["C"], texts=[lyric("Chorus: la la la la la la")]),
        bar(10),
        bar(11),
        bar(12),
    ]
    bars[8].rehearsal = []
    bars[7].dal_segno = True
    return Score(bars, [], [])


def test_parts_are_named_by_where_they_fall() -> None:
    found = {(p.name, p.first, p.last) for p in piano.parts(song_score())}
    assert ("intro", 1, 2) in found
    assert ("interlude", 5, 6) in found
    assert ("outro", 11, 12) in found


def test_a_last_section_with_no_twin_leaves_an_outro() -> None:
    bars = [
        bar(1),
        rest(2, rehearsal=["A"], texts=[lyric("Verse1: a b")]),
        rest(3),
        rest(4, texts=[lyric("Chorus-b: c d")]),
        rest(5),
        bar(6),
        bar(7),
    ]
    assert [(p.name, p.first, p.last) for p in piano.parts(Score(bars, [], []))] == [("intro", 1, 1), ("outro", 6, 7)]


def test_sung_bars_lose_notes_and_kept_bars_keep_them() -> None:
    score = song_score()
    piano.reduce(score)
    assert score.bars[0].voices[0][0].heads
    assert not score.bars[3].voices[0][0].heads
    assert score.bars[3].voices[0][0].hidden
    assert score.bars[4].voices[0][0].heads


def test_second_staff_goes_and_a_piano_cue_chooses_it() -> None:
    score = Score(
        [
            bar(1, voices2=[[note()]], texts2=[Text(0.0, 0.0, [("PIANO ONLY", True, False)], above=False)]),
            rest(2, rehearsal=["A"], texts=[lyric("Verse1: a b")]),
        ],
        [],
        [],
        has_second_staff=True,
    )
    score.bars[0].voices = [[Event(1.0, [], Fraction(4))]]
    found = piano.reduce(score)
    assert found[0].staff == 2
    assert not score.has_second_staff
    assert score.bars[0].voices2 == []
    assert score.bars[0].voices[0][0].heads
    assert [t.runs[0][0] for t in score.bars[0].texts] == ["PIANO ONLY"]


def test_top_staff_is_kept_without_a_cue() -> None:
    assert piano.staff_of([bar(1, voices2=[[note()]])]) == 1


def test_hint_lyric_is_shortened_and_its_label_stays() -> None:
    text = lyric("Verse1: one two three four five six")
    piano.shorten(text)
    whole = "".join(run[0] for run in text.runs)
    assert whole.startswith("Verse1:")
    assert "four" in whole
    assert "five" not in whole


def test_a_plain_cue_is_never_shortened() -> None:
    text = Text(0.0, 0.0, [("one two three four five six seven", False, False)], above=False)
    piano.shorten(text)
    assert text.runs[0][0] == "one two three four five six seven"


def test_a_tie_into_an_emptied_bar_is_removed() -> None:
    first = bar(1)
    first.voices[0][0].heads[0].tie_start = True
    second = bar(2)
    second.voices[0][0].heads[0].tie_stop = True
    second.rehearsal = ["A"]
    second.texts = [lyric("Verse1: a b")]
    score = Score([first, second], [], [])
    piano.reduce(score)
    assert not score.bars[0].voices[0][0].heads[0].tie_start


def test_a_kept_tie_stays() -> None:
    first, second = bar(1), bar(2)
    first.voices[0][0].heads[0].tie_start = True
    second.voices[0][0].heads[0].tie_stop = True
    score = Score([first, second], [], [])
    piano.clean_ties(score)
    assert score.bars[0].voices[0][0].heads[0].tie_start


@pytest.mark.parametrize(
    ("count", "length", "time", "want"),
    [
        (1, Fraction(4), (4, 4), [Fraction(0)]),
        (2, Fraction(4), (4, 4), [Fraction(0), Fraction(2)]),
        (2, Fraction(2), (2, 4), [Fraction(0), Fraction(1)]),
        (4, Fraction(2), (2, 4), [Fraction(0), Fraction(1, 2), Fraction(1), Fraction(3, 2)]),
        (2, Fraction(3), (6, 8), [Fraction(0), Fraction(3, 2)]),
        (3, Fraction(4), (4, 4), [Fraction(0), Fraction(1), Fraction(3)]),
    ],
)
def test_chords_of_an_empty_bar_land_on_real_beats(
    count: int, length: Fraction, time: tuple[int, int], want: list[Fraction]
) -> None:
    assert musicxml.empty_bar_onsets(count, length, time) == want


def chord(x: float) -> Chord:
    return Chord(x, "C", 0, "")


def test_a_lone_chord_in_a_silent_bar_is_on_beat_1_wherever_it_prints() -> None:
    assert [t for t, _ in musicxml.chord_onsets(rest(1, chords=[chord(30.0)]), (4, 4))] == [Fraction(0)]


def test_a_lone_chord_exactly_over_a_late_note_follows_it() -> None:
    held = Bar(
        1,
        0,
        0.0,
        100.0,
        Fraction(4),
        voices=[
            [
                Event(10.0, [Head(10.0, 0.0, "C", 5)], Fraction(2)),
                Event(60.0, [Head(60.0, 0.0, "C", 5)], Fraction(2), onset=Fraction(2)),
            ]
        ],
        chords=[chord(60.0)],
    )
    assert [t for t, _ in musicxml.chord_onsets(held, (4, 4))] == [Fraction(2)]


def test_a_lone_chord_between_notes_opens_the_bar() -> None:
    held = Bar(
        1,
        0,
        0.0,
        100.0,
        Fraction(4),
        voices=[
            [
                Event(10.0, [Head(10.0, 0.0, "C", 5)], Fraction(1)),
                Event(30.0, [Head(30.0, 0.0, "C", 5)], Fraction(3), onset=Fraction(1)),
            ]
        ],
        chords=[chord(25.0)],
    )
    assert [t for t, _ in musicxml.chord_onsets(held, (4, 4))] == [Fraction(0)]


def test_a_lone_chord_opens_the_bar_even_after_a_leading_rest() -> None:
    held = Bar(
        1,
        0,
        0.0,
        100.0,
        Fraction(4),
        voices=[[Event(10.0, [], Fraction(1)), Event(30.0, [Head(30.0, 0.0, "C", 5)], Fraction(3), onset=Fraction(1))]],
        chords=[chord(30.0)],
    )
    assert [t for t, _ in musicxml.chord_onsets(held, (4, 4))] == [Fraction(0)]


def write_score(path: Path, voice: str, staves: int = 1, extra: str = "") -> Path:
    measure = f"<Measure>{extra}<voice>{voice}</voice></Measure>"
    staff = "".join(f'<Staff id="{i + 1}">{measure}</Staff>' for i in range(staves))
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("s.mscx", f'<museScore version="4.70"><Score><Part/>{staff}</Score></museScore>')
    return path


HARMONY_LATE = (
    "<Rest><durationType>measure</durationType><duration>1/1</duration></Rest>"
    "<location><fractions>-7/8</fractions></location><Harmony><harmonyInfo><root>14</root></harmonyInfo></Harmony>"
)
HARMONY_ON_1 = (
    "<Harmony><root>14</root></Harmony><Rest><durationType>measure</durationType><duration>1/1</duration></Rest>"
)


def test_saved_chord_position_is_read_in_both_formats(tmp_path: Path) -> None:
    late = scorefile.read(write_score(tmp_path / "a.mscz", HARMONY_LATE))[0][0]
    assert late.chords == [(Fraction(1, 2), 14, -1, "")]
    first = scorefile.read(write_score(tmp_path / "b.mscz", HARMONY_ON_1))[0][0]
    assert first.chords == [(Fraction(0), 14, -1, "")]


def test_triplets_are_read_at_their_real_length(tmp_path: Path) -> None:
    three = (
        "<Tuplet><normalNotes>2</normalNotes><actualNotes>3</actualNotes></Tuplet>"
        + "<Chord><durationType>eighth</durationType><Note><pitch>60</pitch></Note></Chord>" * 3
        + "<endTuplet/><Harmony><root>14</root></Harmony>"
    )
    assert scorefile.read(write_score(tmp_path / "t.mscz", three))[0][0].chords[0][0] == Fraction(1)


def test_a_bass_note_is_read_under_either_format_name(tmp_path: Path) -> None:
    new = (
        "<Harmony><harmonyInfo><root>14</root><bass>12</bass></harmonyInfo></Harmony>"
        "<Rest><durationType>measure</durationType><duration>1/1</duration></Rest>"
    )
    old = (
        "<Harmony><root>14</root><base>12</base></Harmony>"
        "<Rest><durationType>measure</durationType><duration>1/1</duration></Rest>"
    )
    assert scorefile.difference(write_score(tmp_path / "n.mscz", new), write_score(tmp_path / "o.mscz", old)) == []


def test_two_files_that_disagree_are_named(tmp_path: Path) -> None:
    a = write_score(tmp_path / "a.mscz", HARMONY_ON_1)
    b = write_score(tmp_path / "b.mscz", HARMONY_LATE)
    assert "chords differ" in scorefile.difference(a, b)[0]
    assert scorefile.difference(a, a) == []
    c = write_score(tmp_path / "c.mscz", HARMONY_ON_1, extra="<Marker><label>segno</label></Marker>")
    assert "signs differ" in scorefile.difference(a, c)[0]
    d = write_score(tmp_path / "d.mscz", HARMONY_ON_1, extra="<Jump><jumpTo>segno</jumpTo></Jump>")
    assert "jumps differ" in scorefile.difference(a, d)[0]
    e = write_score(tmp_path / "e.mscz", "<Chord><durationType>whole</durationType><Note><pitch>60</pitch></Note></Chord>")
    f = write_score(tmp_path / "f.mscz", "<Chord><durationType>whole</durationType><Note><pitch>62</pitch></Note></Chord>")
    assert "notes differ" in scorefile.difference(e, f)[0]


def test_a_raw_character_is_not_a_built_in_sign(tmp_path: Path) -> None:
    good = write_score(
        tmp_path / "g.mscz",
        HARMONY_ON_1,
        extra=(
            "<Marker><frameType>1</frameType><frameFgColor r='220' g='0' b='0' a='255'/><size>25</size>"
            "<text><sym>segno</sym></text><label>segno</label><markerType>segno</markerType></Marker>"
        ),
    )
    assert scorefile.builtin_sign_problems(good) == []
    raw = write_score(tmp_path / "r.mscz", HARMONY_ON_1, extra="<Marker><text>x</text><label>segno</label></Marker>")
    assert scorefile.builtin_sign_problems(raw)[0] == "segno is not the built-in sign"
    untyped = write_score(
        tmp_path / "u.mscz",
        HARMONY_ON_1,
        extra="<Marker><text><sym>segno</sym></text><label>segno</label></Marker>",
    )
    assert "no marker type" in scorefile.builtin_sign_problems(untyped)[0]
    forced = write_score(
        tmp_path / "f.mscz",
        HARMONY_ON_1,
        extra=(
            "<Marker><frameType>1</frameType><frameFgColor r='0' g='0' b='220' a='255'/><family>Bravura Text</family>"
            "<text><sym>coda</sym></text><label>codab</label><markerType>codab</markerType></Marker>"
        ),
    )
    assert scorefile.builtin_sign_problems(forced) == ["codab forces a font"]


SONG = Song("Name", "Dm", -1, "Em", 1, "up", 2, "Name")
SAME = Song("Same", "Em", 1, "Em", 1, "", 0, "Same")


def test_a_folder_holds_4_files_or_3_when_the_original_is_the_tone() -> None:
    assert len(SONG.files()) == 4
    assert len(SAME.files()) == 3


def test_missing_and_stray_files_are_named(tmp_path: Path) -> None:
    (tmp_path / "Name").mkdir()
    for name in SONG.files():
        (tmp_path / name).write_text("x")
    assert gate.folder_problems(tmp_path, [SONG]) == []
    (tmp_path / SONG.tone_pdf).unlink()
    assert gate.folder_problems(tmp_path, [SONG]) == [f"missing {SONG.tone_pdf}"]
    (tmp_path / SONG.tone_pdf).write_text("x")
    assert gate.folder_problems(tmp_path, [SONG]) == []
    (tmp_path / "old.pdf").write_text("x")
    assert gate.folder_problems(tmp_path, [SONG]) == ["stray old.pdf"]


def test_piano_sheet_is_judged_by_what_it_holds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        gate,
        "decode",
        lambda _: Score([rest(1, rehearsal=["A"], texts=[lyric("Verse1: a")], chords=[chord(1.0)])], [], []),
    )
    hidden = (
        "<Harmony><root>14</root></Harmony>"
        "<Rest><visible>0</visible><durationType>measure</durationType><duration>1/1</duration></Rest>"
    )
    assert gate.piano_problems(tmp_path, write_score(tmp_path / "ok.mscz", hidden)) == []
    assert gate.piano_problems(tmp_path, write_score(tmp_path / "two.mscz", hidden, staves=2)) == ["2 staves"]
    shown = "<Harmony><root>14</root></Harmony><Rest><durationType>measure</durationType><duration>1/1</duration></Rest>"
    assert gate.piano_problems(tmp_path, write_score(tmp_path / "shown.mscz", shown)) == ["bar 1: rest not hidden"]
    noted = (
        "<Harmony><root>14</root></Harmony><Chord><durationType>whole</durationType><Note><pitch>60</pitch></Note></Chord>"
    )
    assert gate.piano_problems(tmp_path, write_score(tmp_path / "noted.mscz", noted)) == [
        "bar 1: notes outside the kept parts",
    ]
    assert gate.piano_problems(
        tmp_path,
        write_score(
            tmp_path / "none.mscz",
            "<Rest><visible>0</visible><durationType>measure</durationType><duration>1/1</duration></Rest>",
        ),
    ) == ["bar 1: 0 chords, source has 1"]


def test_a_late_chord_in_a_saved_score_fails_the_beat_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(verify, "decode", lambda _: Score([rest(1, time=(4, 4), chords=[chord(30.0)])], [], []))
    assert verify.chord_beat_problems(tmp_path, write_score(tmp_path / "ok.mscz", HARMONY_ON_1)) == []
    assert "due on" in verify.chord_beat_problems(tmp_path, write_score(tmp_path / "late.mscz", HARMONY_LATE))[0]


def test_port_module_names_the_3_sheets() -> None:
    assert {port.ORIGINAL, port.TONE, port.PIANO} == {"original", "tone", "piano"}


def test_two_chords_in_a_silent_bar_land_on_beats_wherever_they_print() -> None:
    silent = rest(1, chords=[chord(9.0), chord(21.0)])
    assert [t for t, _ in musicxml.chord_onsets(silent, (4, 4))] == [Fraction(0), Fraction(2)]


def test_a_hint_lyric_in_several_runs_still_stops_at_its_word_limit() -> None:
    text = Text(
        0.0,
        0.0,
        [("Verse1: ", True, True), ("one two three", False, True), (" four five six", False, False)],
        above=False,
    )
    piano.shorten(text)
    whole = "".join(run[0] for run in text.runs)
    assert "four" in whole
    assert "five" not in whole


def test_a_cue_stacking_2_labels_keeps_words_for_each() -> None:
    text = Text(
        0.0,
        0.0,
        [
            ("Verse1: ", True, True),
            ("one two three four five", False, True),
            ("Verse2: ", True, True),
            ("six seven eight nine ten", False, True),
        ],
        above=False,
    )
    piano.shorten(text)
    whole = "".join(run[0] for run in text.runs)
    assert "four" in whole
    assert "five" not in whole
    assert "nine" in whole
    assert "ten" not in whole


def test_a_band_cue_sharing_the_lyric_text_survives_the_word_limit() -> None:
    text = Text(
        0.0,
        0.0,
        [
            ("Chorus1: ", True, True),
            ("one two three four five", False, True),
            ("\nBAND IN", True, True),
            ("\nChorus2: ", True, True),
            ("six seven", False, True),
        ],
        above=False,
    )
    piano.shorten(text)
    whole = "".join(run[0] for run in text.runs)
    assert "BAND IN" in whole
    assert "five" not in whole
    assert "seven" in whole


def test_a_lone_line_is_found_and_only_it() -> None:
    assert piano.lone_lines([1, 9, 10, 14], 17) == [9]
    assert piano.lone_lines([1, 9], 9) == [9]
    assert piano.lone_lines([1, 5], 8) == []
