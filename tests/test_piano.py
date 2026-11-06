"""The piano sheet, chord timing, song folders and what a saved score holds."""
import zipfile
from fractions import Fraction as F

import pytest

from muse import gate, musicxml, piano, port, scorefile, verify
from muse.model import Bar, Chord, Event, Head, Score, Text
from muse.song import Song


def note(x=1.0, length=F(4)):
    return Event(x, [Head(x, 0.0, "C", 5)], length)


def bar(number, **kw):
    return Bar(number, 0, 0.0, 100.0, F(4), voices=[[note()]], **kw)


def rest(number, **kw):
    return Bar(number, 0, 0.0, 100.0, F(4), voices=[[Event(0.0, [], F(4), bar_rest=True)]], **kw)


def lyric(words):
    label, rest_of = words.split(":")
    return Text(0.0, 0.0, [(label + ": ", True, True), (rest_of, False, True)], False)


def song_score():
    bars = [bar(1), bar(2), rest(3, rehearsal=["A"], texts=[lyric("Verse1: one two three four five six")]), bar(4), bar(5, rehearsal=["Interlude"]), bar(6),
            bar(7, rehearsal=["B"], texts=[lyric("Chorus: la la la la la la")]), bar(8), bar(9, rehearsal=["C"], texts=[lyric("Chorus: la la la la la la")]), bar(10), bar(11), bar(12)]
    bars[8].rehearsal = []
    bars[7].dal_segno = True
    return Score(bars, [], [])


def test_parts_are_named_by_where_they_fall():
    found = {(p.name, p.first, p.last) for p in piano.parts(song_score())}
    assert ("intro", 1, 2) in found
    assert ("interlude", 5, 6) in found
    assert ("outro", 11, 12) in found


def test_a_last_section_with_no_twin_leaves_an_outro():
    bars = [bar(1), rest(2, rehearsal=["A"], texts=[lyric("Verse1: a b")]), rest(3), rest(4, texts=[lyric("Chorus-b: c d")]), rest(5), bar(6), bar(7)]
    assert [(p.name, p.first, p.last) for p in piano.parts(Score(bars, [], []))] == [("intro", 1, 1), ("outro", 6, 7)]


def test_sung_bars_lose_notes_and_kept_bars_keep_them():
    score = song_score()
    piano.reduce(score)
    assert score.bars[0].voices[0][0].heads
    assert not score.bars[3].voices[0][0].heads
    assert score.bars[3].voices[0][0].hidden
    assert score.bars[4].voices[0][0].heads


def test_second_staff_goes_and_a_piano_cue_chooses_it():
    score = Score([bar(1, voices2=[[note()]], texts2=[Text(0.0, 0.0, [("PIANO ONLY", True, False)], False)]), rest(2, rehearsal=["A"], texts=[lyric("Verse1: a b")])], [], [], has_second_staff=True)
    score.bars[0].voices = [[Event(1.0, [], F(4))]]
    found = piano.reduce(score)
    assert found[0].staff == 2
    assert not score.has_second_staff
    assert score.bars[0].voices2 == []
    assert score.bars[0].voices[0][0].heads
    assert [t.runs[0][0] for t in score.bars[0].texts] == ["PIANO ONLY"]


def test_top_staff_is_kept_without_a_cue():
    assert piano.staff_of([bar(1, voices2=[[note()]])]) == 1


def test_hint_lyric_is_shortened_and_its_label_stays():
    text = lyric("Verse1: one two three four five six")
    piano.shorten(text)
    whole = "".join(run[0] for run in text.runs)
    assert whole.startswith("Verse1:") and "four" in whole and "five" not in whole


def test_a_plain_cue_is_never_shortened():
    text = Text(0.0, 0.0, [("one two three four five six seven", False, False)], False)
    piano.shorten(text)
    assert text.runs[0][0] == "one two three four five six seven"


def test_a_tie_into_an_emptied_bar_is_removed():
    first = bar(1)
    first.voices[0][0].heads[0].tie_start = True
    second = bar(2)
    second.voices[0][0].heads[0].tie_stop = True
    second.rehearsal = ["A"]
    second.texts = [lyric("Verse1: a b")]
    score = Score([first, second], [], [])
    piano.reduce(score)
    assert not score.bars[0].voices[0][0].heads[0].tie_start


def test_a_kept_tie_stays():
    first, second = bar(1), bar(2)
    first.voices[0][0].heads[0].tie_start = True
    second.voices[0][0].heads[0].tie_stop = True
    score = Score([first, second], [], [])
    piano.clean_ties(score)
    assert score.bars[0].voices[0][0].heads[0].tie_start


@pytest.mark.parametrize(("count", "length", "time", "want"), [
    (1, F(4), (4, 4), [F(0)]), (2, F(4), (4, 4), [F(0), F(2)]), (2, F(2), (2, 4), [F(0), F(1)]),
    (4, F(2), (2, 4), [F(0), F(1, 2), F(1), F(3, 2)]), (2, F(3), (6, 8), [F(0), F(3, 2)]), (3, F(4), (4, 4), [F(0), F(1), F(3)])])
def test_chords_of_an_empty_bar_land_on_real_beats(count, length, time, want):
    assert musicxml.empty_bar_onsets(count, length, time) == want


def chord(x):
    return Chord(x, "C", 0, "")


def test_a_lone_chord_in_a_silent_bar_is_on_beat_1_wherever_it_prints():
    assert [t for t, _ in musicxml.chord_onsets(rest(1, chords=[chord(30.0)]), (4, 4))] == [F(0)]


def test_a_lone_chord_exactly_over_a_late_note_follows_it():
    held = Bar(1, 0, 0.0, 100.0, F(4), voices=[[Event(10.0, [Head(10.0, 0.0, "C", 5)], F(2)), Event(60.0, [Head(60.0, 0.0, "C", 5)], F(2), onset=F(2))]], chords=[chord(60.0)])
    assert [t for t, _ in musicxml.chord_onsets(held, (4, 4))] == [F(2)]


def test_a_lone_chord_between_notes_opens_the_bar():
    held = Bar(1, 0, 0.0, 100.0, F(4), voices=[[Event(10.0, [Head(10.0, 0.0, "C", 5)], F(1)), Event(30.0, [Head(30.0, 0.0, "C", 5)], F(3), onset=F(1))]], chords=[chord(25.0)])
    assert [t for t, _ in musicxml.chord_onsets(held, (4, 4))] == [F(0)]


def test_a_lone_chord_opens_the_bar_even_after_a_leading_rest():
    held = Bar(1, 0, 0.0, 100.0, F(4), voices=[[Event(10.0, [], F(1)), Event(30.0, [Head(30.0, 0.0, "C", 5)], F(3), onset=F(1))]], chords=[chord(30.0)])
    assert [t for t, _ in musicxml.chord_onsets(held, (4, 4))] == [F(0)]


def write_score(path, voice, staves=1, extra=""):
    measure = f"<Measure>{extra}<voice>{voice}</voice></Measure>"
    staff = "".join(f'<Staff id="{i + 1}">{measure}</Staff>' for i in range(staves))
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("s.mscx", f'<museScore version="4.70"><Score><Part/>{staff}</Score></museScore>')
    return path


HARMONY_LATE = "<Rest><durationType>measure</durationType><duration>1/1</duration></Rest><location><fractions>-7/8</fractions></location><Harmony><harmonyInfo><root>14</root></harmonyInfo></Harmony>"
HARMONY_ON_1 = "<Harmony><root>14</root></Harmony><Rest><durationType>measure</durationType><duration>1/1</duration></Rest>"


def test_saved_chord_position_is_read_in_both_formats(tmp_path):
    late = scorefile.read(write_score(tmp_path / "a.mscz", HARMONY_LATE))[0][0]
    assert late.chords == [(F(1, 2), 14, -1, "")]
    first = scorefile.read(write_score(tmp_path / "b.mscz", HARMONY_ON_1))[0][0]
    assert first.chords == [(F(0), 14, -1, "")]


def test_triplets_are_read_at_their_real_length(tmp_path):
    three = "<Tuplet><normalNotes>2</normalNotes><actualNotes>3</actualNotes></Tuplet>" + "<Chord><durationType>eighth</durationType><Note><pitch>60</pitch></Note></Chord>" * 3 + "<endTuplet/><Harmony><root>14</root></Harmony>"
    assert scorefile.read(write_score(tmp_path / "t.mscz", three))[0][0].chords[0][0] == F(1)


def test_a_bass_note_is_read_under_either_format_name(tmp_path):
    new = "<Harmony><harmonyInfo><root>14</root><bass>12</bass></harmonyInfo></Harmony><Rest><durationType>measure</durationType><duration>1/1</duration></Rest>"
    old = "<Harmony><root>14</root><base>12</base></Harmony><Rest><durationType>measure</durationType><duration>1/1</duration></Rest>"
    assert scorefile.difference(write_score(tmp_path / "n.mscz", new), write_score(tmp_path / "o.mscz", old)) == []


def test_two_files_that_disagree_are_named(tmp_path):
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


def test_a_raw_character_is_not_a_built_in_sign(tmp_path):
    good = write_score(tmp_path / "g.mscz", HARMONY_ON_1, extra="<Marker><frameType>1</frameType><frameFgColor r='220' g='0' b='0' a='255'/><size>25</size><text><sym>segno</sym></text><label>segno</label><markerType>segno</markerType></Marker>")
    assert scorefile.builtin_sign_problems(good) == []
    raw = write_score(tmp_path / "r.mscz", HARMONY_ON_1, extra="<Marker><text>x</text><label>segno</label></Marker>")
    assert scorefile.builtin_sign_problems(raw)[0] == "segno is not the built-in sign"
    untyped = write_score(tmp_path / "u.mscz", HARMONY_ON_1, extra="<Marker><text><sym>segno</sym></text><label>segno</label></Marker>")
    assert "no marker type" in scorefile.builtin_sign_problems(untyped)[0]
    forced = write_score(tmp_path / "f.mscz", HARMONY_ON_1, extra="<Marker><frameType>1</frameType><frameFgColor r='0' g='0' b='220' a='255'/><family>Bravura Text</family><text><sym>coda</sym></text><label>codab</label><markerType>codab</markerType></Marker>")
    assert scorefile.builtin_sign_problems(forced) == ["codab forces a font"]


SONG = Song("Name", "Dm", -1, "Em", 1, "up", 2, "Name")
SAME = Song("Same", "Em", 1, "Em", 1, "", 0, "Same")


def test_a_folder_holds_4_files_or_3_when_the_original_is_the_tone():
    assert len(SONG.files()) == 4
    assert len(SAME.files()) == 3
    assert SONG.legacy_file not in SONG.files()


def test_missing_and_stray_files_are_named(tmp_path):
    (tmp_path / "Name").mkdir()
    for name in SONG.files():
        (tmp_path / name).write_text("x")
    assert gate.folder_problems(tmp_path, [SONG], set()) == []
    (tmp_path / SONG.tone_pdf).unlink()
    assert gate.folder_problems(tmp_path, [SONG], set()) == [f"missing {SONG.tone_pdf}"]
    (tmp_path / SONG.tone_pdf).write_text("x")
    assert gate.folder_problems(tmp_path, [SONG], {"Name"}) == []
    (tmp_path / "old.pdf").write_text("x")
    assert gate.folder_problems(tmp_path, [SONG], {"Name"}) == ["stray old.pdf"]


def test_piano_sheet_is_judged_by_what_it_holds(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "decode", lambda _: Score([rest(1, rehearsal=["A"], texts=[lyric("Verse1: a")], chords=[chord(1.0)])], [], []))
    hidden = "<Harmony><root>14</root></Harmony><Rest><visible>0</visible><durationType>measure</durationType><duration>1/1</duration></Rest>"
    assert gate.piano_problems(tmp_path, write_score(tmp_path / "ok.mscz", hidden)) == []
    assert gate.piano_problems(tmp_path, write_score(tmp_path / "two.mscz", hidden, staves=2)) == ["2 staves"]
    shown = "<Harmony><root>14</root></Harmony><Rest><durationType>measure</durationType><duration>1/1</duration></Rest>"
    assert gate.piano_problems(tmp_path, write_score(tmp_path / "shown.mscz", shown)) == ["bar 1: rest not hidden"]
    noted = "<Harmony><root>14</root></Harmony><Chord><durationType>whole</durationType><Note><pitch>60</pitch></Note></Chord>"
    assert gate.piano_problems(tmp_path, write_score(tmp_path / "noted.mscz", noted)) == ["bar 1: notes outside the kept parts"]
    assert gate.piano_problems(tmp_path, write_score(tmp_path / "none.mscz", "<Rest><visible>0</visible><durationType>measure</durationType><duration>1/1</duration></Rest>")) == ["bar 1: 0 chords, source has 1"]


def test_a_late_chord_in_a_saved_score_fails_the_beat_check(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "decode", lambda _: Score([rest(1, time=(4, 4), chords=[chord(30.0)])], [], []))
    assert verify.chord_beat_problems(tmp_path, write_score(tmp_path / "ok.mscz", HARMONY_ON_1)) == []
    assert "due on" in verify.chord_beat_problems(tmp_path, write_score(tmp_path / "late.mscz", HARMONY_LATE))[0]


def test_port_module_names_the_3_sheets():
    assert {port.ORIGINAL, port.TONE, port.PIANO} == {"original", "tone", "piano"}


def test_two_chords_in_a_silent_bar_land_on_beats_wherever_they_print():
    silent = rest(1, chords=[chord(9.0), chord(21.0)])
    assert [t for t, _ in musicxml.chord_onsets(silent, (4, 4))] == [F(0), F(2)]


def test_a_hint_lyric_in_several_runs_still_stops_at_its_word_limit():
    text = Text(0.0, 0.0, [("Verse1: ", True, True), ("one two three", False, True), (" four five six", False, False)], False)
    piano.shorten(text)
    whole = "".join(run[0] for run in text.runs)
    assert "four" in whole and "five" not in whole


def test_a_cue_stacking_2_labels_keeps_words_for_each():
    text = Text(0.0, 0.0, [("Verse1: ", True, True), ("one two three four five", False, True), ("Verse2: ", True, True), ("six seven eight nine ten", False, True)], False)
    piano.shorten(text)
    whole = "".join(run[0] for run in text.runs)
    assert "four" in whole and "five" not in whole
    assert "nine" in whole and "ten" not in whole


def test_a_band_cue_sharing_the_lyric_text_survives_the_word_limit():
    text = Text(0.0, 0.0, [("Chorus1: ", True, True), ("one two three four five", False, True), ("\nBAND IN", True, True), ("\nChorus2: ", True, True), ("six seven", False, True)], False)
    piano.shorten(text)
    whole = "".join(run[0] for run in text.runs)
    assert "BAND IN" in whole and "five" not in whole and "seven" in whole


def test_a_lone_line_is_found_and_only_it():
    assert piano.lone_lines([1, 9, 10, 14], 17) == [9]
    assert piano.lone_lines([1, 9], 9) == [9]
    assert piano.lone_lines([1, 5], 8) == []


def test_the_legacy_half_diminished_spelling_is_the_same_chord(tmp_path):
    rest = "<Rest><durationType>measure</durationType><duration>1/1</duration></Rest>"
    short = write_score(tmp_path / "s.mscz", f"<Harmony><root>16</root><name>0</name></Harmony>{rest}")
    legacy = write_score(tmp_path / "l.mscz", f"<Harmony><root>16</root><name>07</name></Harmony>{rest}")
    other = write_score(tmp_path / "o.mscz", f"<Harmony><root>16</root><name>m7</name></Harmony>{rest}")
    assert scorefile.difference(short, legacy) == []
    assert "chords differ" in scorefile.difference(short, other)[0]
