"""The seating extension inside MuseScore, on scores built in code: it measures chord room and fits, evens and seats chords."""
import pathlib
import re
import shutil
from fractions import Fraction as F

import pymupdf
import pytest

from muse import chordseat, layout, pdfcheck, port
from muse.model import Bar, Chord, Event, Head, Score
from muse.render import BATCHER

pytestmark = [pytest.mark.musescore, pytest.mark.skipif(shutil.which("mscore") is None, reason="MuseScore runs only where it is installed; CI installs it")]


def chord(root: str, kind: str = "") -> Chord:
    return Chord(0.0, root, 0, kind)


def held(number: int, chords: list[Chord]) -> Bar:
    """A bar of 1 whole note carrying the given chords, the later ones on beats 2 to 4."""
    for at, item in enumerate(chords):
        item.x = 10.0 + at * 20.0
    return Bar(number, 0, 0.0, 100.0, F(4), voices=[[Event(10.0, [Head(10.0, 0.0, "C", 5)], F(4))]], chords=chords)


def imported(tmp_path: pathlib.Path, bars: list[Bar], lines: set[int]) -> pathlib.Path:
    score = Score(bars, [], [], minor=True)
    for bar in score.bars:
        bar.new_system = bar.number in lines and bar.number != 1
    style = port.styled(tmp_path, "s.mss", "Fixture", 1.6, port.BASE_DISTANCE)
    source = port.imported(score, tmp_path, "fixture", style, False)
    target = tmp_path / "fixture.mscz"
    port.edited(source, target, 1.6, port.BASE_DISTANCE, score)
    layout.rewrite(target, layout.placed)
    return target


def test_measure_mode_gives_a_crowded_bar_a_share_and_a_roomy_bar_none(tmp_path: pathlib.Path) -> None:
    bars = [held(1, [chord("C")]), held(2, [chord("C", "maj7"), chord("D", "m7"), chord("E", "m7"), chord("F", "maj7")]), held(3, [chord("G")])]
    natural = imported(tmp_path, bars, {1})
    layout.rewrite(natural, layout.unjustified)
    shares = layout.chord_shares(natural, 3, [0, 1, 2])
    assert shares[0] == pytest.approx(1.0) and shares[2] == pytest.approx(1.0)
    assert shares[1] > 1.2


def test_seat_mode_widens_a_crowded_bar_from_its_neighbours_and_seats_beat_1_chords(tmp_path: pathlib.Path) -> None:
    crowded = [chord("C", "maj7"), chord("D", "m7"), chord("E", "m7"), chord("F", "maj7")]
    bars = [held(1, [chord("C")]), held(2, crowded), held(3, [chord("G")]), held(4, [chord("A", "m")])]
    score_path = imported(tmp_path, bars, {1, 5})  # 1 line: the crowded bar must take room from its neighbours
    pdf = score_path.with_suffix(".pdf")

    def rendered() -> pathlib.Path:
        BATCHER.render(score_path, [pdf, pdf.with_suffix(".mpos")], None)
        return pdf

    layout.seat_inked(score_path, rendered, lambda probe: True)
    assert layout.read_tag(score_path, "museSeatError") == ""
    assert pdfcheck.chord_collisions(pdf) == []
    assert chordseat.problems(score_path, pdf) == []


def test_the_extension_refuses_without_its_gap(tmp_path: pathlib.Path) -> None:
    score_path = imported(tmp_path, [held(1, [chord("C")])], {1})
    layout.run_extension(score_path, score_path)
    assert "museChordGap" in layout.read_tag(score_path, "museSeatError")


def chord_heights(pdf: pathlib.Path) -> list[list[float]]:
    """Per page, per staff in order, how far its chords' baseline sits above its staff top, in points."""
    from muse import pdftext

    found = []
    for page in pdftext.open(pdf):
        staves = chordseat.staves_of(chordseat.segments(page))
        size = chordseat.chord_size(pdf)
        bases = [span["origin"][1] for block in page.get_text("dict")["blocks"] for line in block.get("lines", []) for span in line["spans"] if abs(span["size"] - size) < 0.05 and span["text"].strip()]
        heights = []
        for at, staff in enumerate(staves):
            floor = staves[at - 1][-1] if at else 0.0
            mine = [base for base in bases if floor < base < staff[0]]
            heights.append(staff[0] - min(mine) if mine else 0.0)
        found.append(heights)
    return found


def test_a_high_note_lifts_only_its_own_line_never_a_line_at_the_same_height_on_another_page(tmp_path: pathlib.Path) -> None:
    bars = [held(n, [chord("C")]) for n in range(1, 41)]
    bars[0].voices = [[Event(10.0, [Head(10.0, 0.0, "A", 6)], F(4))]]  # on ledger lines far above the staff: its line's chords rise over it
    score_path = imported(tmp_path, bars, set(range(1, 41, 2)))  # 2 bars a line, 20 lines: 2 pages
    layout.set_tags(score_path, {"museChordGap": f"{layout.CLEAR_SPACES:.4f}"})
    layout.run_extension(score_path, score_path)
    pdf = score_path.with_suffix(".pdf")
    BATCHER.render(score_path, [pdf], None)
    heights = chord_heights(pdf)
    assert len(heights) >= 2, "the fixture must reach a second page"
    ordinary = heights[0][1]
    assert heights[1][0] == pytest.approx(ordinary, abs=0.5)  # page 2's first line shares page 1's first-line height, never its lift


def test_each_version_installs_apart(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "extensions"
    monkeypatch.setenv("MUSE_EXTENSIONS", str(root))
    first = layout.install_extension()
    source = tmp_path / "copy"
    shutil.copytree(layout.EXTENSION, source)
    (source / "main.js").write_text((source / "main.js").read_text() + "\n// another version\n")
    monkeypatch.setattr(layout, "EXTENSION", source)
    second = layout.install_extension()
    assert first != second
    held = {path.name for path in root.iterdir()}
    assert len(held) == 2 and all(name.startswith(layout.EXTENSION_FOLDER + "-") for name in held)


def resting(number: int, chords: list[Chord], texts: list[object] | None = None) -> Bar:
    """A bar holding only a whole rest, carrying the given chords and cues."""
    return Bar(number, 0, 0.0, 100.0, F(4), voices=[[Event(0.0, [], F(4), bar_rest=True)]], chords=chords, texts=texts or [])  # type: ignore[arg-type]


def test_a_cue_rises_above_the_chords_and_the_chords_stay_close_to_an_empty_staff(tmp_path: pathlib.Path) -> None:
    from muse.model import Text

    cue = Text(10.0, -20.0, [("Only Piano", True, True)], True, "cue", 10.0)
    bars = [resting(1, [chord("C", "maj7")], [cue]), resting(2, [chord("A", "m")]), resting(3, [chord("F")]), resting(4, [chord("G")])]  # a name wide enough to reach over the cue
    score_path = imported(tmp_path, bars, {1, 5})
    pdf = score_path.with_suffix(".pdf")

    def rendered() -> pathlib.Path:
        BATCHER.render(score_path, [pdf, pdf.with_suffix(".mpos")], None)
        return pdf

    layout.seat_inked(score_path, rendered, lambda probe: True)
    assert pdfcheck.seating_problems(pdf) == []
    assert pdfcheck.text_collisions(pdf) == []


def test_cues_sharing_a_place_stack_above_the_chords_and_never_overprint(tmp_path: pathlib.Path) -> None:
    from muse.model import Text

    cues = [Text(10.0, -20.0, [(words, True, True)], True, "cue", 10.0) for words in ("PIANO ONLY", "Slow Strings", "BAND IN")]
    bars = [resting(1, [chord("C", "maj7")], cues), resting(2, [chord("A", "m")]), resting(3, [chord("F")]), resting(4, [chord("G")])]
    score_path = imported(tmp_path, bars, {1, 5})
    pdf = score_path.with_suffix(".pdf")

    def rendered() -> pathlib.Path:
        BATCHER.render(score_path, [pdf, pdf.with_suffix(".mpos")], None)
        return pdf

    layout.seat_inked(score_path, rendered, lambda probe: True)
    assert pdfcheck.text_collisions(pdf) == []
    assert pdfcheck.seating_problems(pdf) == []


def test_cues_over_the_first_line_stay_on_the_page(tmp_path: pathlib.Path) -> None:
    from muse.model import Text

    cues = [Text(10.0, -20.0, [(words, True, True)], True, "cue", 10.0) for words in ("PIANO ONLY", "Ballad", "BAND IN")]
    bars = [resting(1, [chord("C", "maj7")], cues), resting(2, [chord("A", "m")])] + [resting(n, [chord("F")]) for n in range(3, 9)]
    score_path = imported(tmp_path, bars, {1, 3, 5, 7})
    pdf = score_path.with_suffix(".pdf")

    def rendered() -> pathlib.Path:
        BATCHER.render(score_path, [pdf, pdf.with_suffix(".mpos")], None)
        return pdf

    layout.seat_inked(score_path, rendered, lambda probe: True)
    rendered()
    assert pdfcheck.off_page(pdf) == []
    assert pdfcheck.text_collisions(pdf) == []


def test_many_threads_install_1_copy_without_colliding(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import concurrent.futures

    monkeypatch.setenv("MUSE_EXTENSIONS", str(tmp_path))
    with concurrent.futures.ThreadPoolExecutor(16) as pool:
        uris = set(pool.map(lambda _: layout.install_extension(), range(64)))
    assert len(uris) == 1
    assert [path.name for path in tmp_path.iterdir()] == [f"{layout.EXTENSION_FOLDER}-{next(iter(uris)).rsplit('-', 1)[1]}"]


def test_a_cue_on_a_lines_last_bar_ends_inside_the_margin(tmp_path: pathlib.Path) -> None:
    from muse.model import Text

    cue = Text(90.0, -20.0, [("PIANO ONLY FROM HERE", True, True)], True, "cue", 10.0)
    bars = [resting(1, [chord("C")]), resting(2, [chord("A", "m")]), resting(3, [chord("F")]), resting(4, [chord("G")], [cue])]
    score_path = imported(tmp_path, bars, {1, 5})
    pdf = score_path.with_suffix(".pdf")

    def rendered() -> pathlib.Path:
        BATCHER.render(score_path, [pdf, pdf.with_suffix(".mpos")], None)
        return pdf

    layout.seat_inked(score_path, rendered, lambda probe: True)
    assert pdfcheck.past_margin(pdf) == []
    assert pdfcheck.text_collisions(pdf) == []


def test_the_clef_and_signatures_opening_a_line_never_lift_its_chords(tmp_path: pathlib.Path) -> None:
    layout.install_extension()
    bars = [Bar(n, 0, 0.0, 100.0, F(4), voices=[[Event(10.0, [], F(4))]], chords=[chord("F", "m")]) for n in range(1, 5)]
    for bar in bars:
        bar.chords[0].x = 10.0
        bar.chords[0].root_alter = 1
    bars[0].key = 3  # 3 sharps: the top sharp reaches above the staff at the line's start
    score_path = imported(tmp_path, bars, {1})
    layout.set_tags(score_path, {"museChordGap": f"{layout.CLEAR_SPACES:.4f}"})
    layout.run_extension(score_path, score_path)
    pdf = score_path.with_suffix(".pdf")
    BATCHER.render(score_path, [pdf, pdf.with_suffix(".mpos")], None)
    assert max(spaces for _, _, spaces, _ in pdfcheck.chord_seating(pdf)) <= 1.0  # seated at the half-space gap over the staff; the clef lifts to near 2, the sharps past 2.3


def test_italic_cues_below_the_staff_stack_rather_than_overprint(tmp_path: pathlib.Path) -> None:
    from muse.model import Text

    bars = [held(n, [chord("C")]) for n in range(1, 9)]
    bars[0].texts = [Text(10.0, 60.0, [("Chorus: a long italic cue running well past the next bar", False, True)], False, "cue", 10.0)]
    bars[1].texts = [Text(10.0, 60.0, [("(Fill)", False, True)], False, "cue", 10.0)]
    score_path = imported(tmp_path, bars, {1})
    pdf = score_path.with_suffix(".pdf")
    BATCHER.render(score_path, [pdf], None)
    assert pdfcheck.text_collisions(pdf) == []


def test_a_sign_before_a_beat_1_chord_asks_its_bar_to_grow_by_the_sign_never_a_hundredfold(tmp_path: pathlib.Path) -> None:
    layout.install_extension()
    bars = [held(n, [chord("F", "maj7")]) for n in range(1, 5)]
    bars[1].segno = True  # the sign takes fixed room before the chord
    natural = imported(tmp_path, bars, {1})
    layout.rewrite(natural, layout.unjustified)
    shares = layout.chord_shares(natural, 4, [0, 1, 2, 3])
    assert shares[1] < 4.0, shares


def test_a_sign_opening_a_line_clears_its_bar_number_and_its_chord_follows_it(tmp_path: pathlib.Path) -> None:
    layout.install_extension()
    bars = [held(n, [chord("C", "maj7")]) for n in range(1, 9)]
    bars[4].segno = True  # opens the second line, beside that line's bar number
    bars[4].rehearsal = ["A"]  # a section mark there too, as songs open a section with its sign
    score_path = imported(tmp_path, bars, {1, 5})
    pdf = score_path.with_suffix(".pdf")

    def rendered() -> pathlib.Path:
        BATCHER.render(score_path, [pdf, pdf.with_suffix(".mpos")], None)
        return pdf

    layout.seat_inked(score_path, rendered, lambda probe: True)
    assert pdfcheck.sign_text_overlaps(pdf) == []
    assert chordseat.problems(score_path, pdf) == []


def test_a_chord_after_a_sign_mid_line_starts_past_its_box(tmp_path: pathlib.Path) -> None:
    layout.install_extension()
    bars = [held(n, [chord("F", "maj7")]) for n in range(1, 9)]
    bars[2].coda = True  # mid-line: the chord on its beat 1 follows the box
    score_path = imported(tmp_path, bars, {1, 5})
    pdf = score_path.with_suffix(".pdf")

    def rendered() -> pathlib.Path:
        BATCHER.render(score_path, [pdf, pdf.with_suffix(".mpos")], None)
        return pdf

    layout.seat_inked(score_path, rendered, lambda probe: True)
    assert pdfcheck.sign_text_overlaps(pdf) == []



def test_raised_cues_reserve_their_height_so_they_never_reach_the_line_above(tmp_path: pathlib.Path) -> None:
    from muse.model import Text

    layout.install_extension()
    bars = [held(n, [chord("C", "maj7")]) for n in range(1, 9)]
    bars[1].texts = [Text(10.0, 60.0, [("Below line 1: a long cue", False, True)], False, "cue", 10.0)]
    bars[5].texts = [Text(10.0, -60.0, [(f"Cue {at} stacked over line 2", True, False)], True, "cue", 14.0) for at in range(1, 7)]  # a stack taller than the gap between lines
    score_path = imported(tmp_path, bars, {1, 5})
    pdf = score_path.with_suffix(".pdf")

    def rendered() -> pathlib.Path:
        BATCHER.render(score_path, [pdf, pdf.with_suffix(".mpos")], None)
        return pdf

    layout.seat_inked(score_path, rendered, lambda probe: True)
    assert pdfcheck.text_collisions(pdf) == []


def test_a_word_over_a_dynamic_is_text_on_text(tmp_path: pathlib.Path) -> None:
    launcher = pathlib.Path(shutil.which("mscore") or "").resolve()
    script = launcher.read_bytes()[:2] == b"#!"
    binary = pathlib.Path(re.findall(r'exec "([^"]+)"', launcher.read_text())[0]) if script else launcher  # a package manager installs a script that execs the app
    font = binary.parent.parent / "Resources" / "fonts" / "BravuraText.otf"
    assert font.exists(), font

    def sheet(over: bool) -> pathlib.Path:
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_font(fontname="bravura", fontfile=str(font))
        page.insert_text((100, 125), chr(0xE52D), fontname="bravura", fontsize=20)  # mf
        page.insert_text((104 if over else 160, 122), "Coda", fontsize=10)
        path = tmp_path / f"dynamic-{over}.pdf"
        doc.save(path)
        return path

    assert pdfcheck.symbol_text_overlaps(sheet(True)) == ["text on text: p1: dynamic or tempo note over 'Coda'"]
    assert pdfcheck.symbol_text_overlaps(sheet(False)) == []


def test_a_volta_number_never_prints_on_a_chord(tmp_path: pathlib.Path) -> None:
    layout.install_extension()
    bars = [Bar(n, 0, 0.0, 100.0, F(4), voices=[[Event(10.0, [], F(4))]], chords=[chord("F", "m")]) for n in range(1, 5)]
    for bar in bars:
        bar.chords[0].x = 10.0
    bars[1].volta, bars[1].volta_stop = "1", "1"  # MuseScore never moves a volta clear of chords placed by hand
    score_path = imported(tmp_path, bars, {1})
    layout.set_tags(score_path, {"museChordGap": f"{layout.CLEAR_SPACES:.4f}"})
    layout.run_extension(score_path, score_path)
    pdf = score_path.with_suffix(".pdf")
    BATCHER.render(score_path, [pdf, pdf.with_suffix(".mpos")], None)
    words = pymupdf.open(pdf)[0].get_text("words")
    numbers = [w for w in words if w[4] == "1."]
    chords = [w for w in words if w[4] == "Fm"]
    assert numbers and len(chords) == 4
    assert pdfcheck.text_collisions(pdf) == []  # judged as the gate judges a sheet

def test_a_slur_far_above_the_staff_lifts_its_line_chords_over_it(tmp_path: pathlib.Path) -> None:
    layout.install_extension()

    def seated(slurred: bool) -> float:
        home = tmp_path / str(slurred)
        home.mkdir()
        bars = [held(n, [chord("C")]) for n in range(1, 5)]
        first, second = Head(10.0, 0.0, "C", 8), Head(30.0, 0.0, "C", 8)  # 3 octaves up: the slur over them tops 10 staff spaces above the staff
        first.slur_start, second.slur_stop = slurred, slurred
        bars[0].voices = [[Event(10.0, [first], F(2)), Event(30.0, [second], F(2))]]
        score_path = imported(home, bars, {1})
        layout.set_tags(score_path, {"museChordGap": f"{layout.CLEAR_SPACES:.4f}"})
        layout.run_extension(score_path, score_path)
        pdf = score_path.with_suffix(".pdf")
        BATCHER.render(score_path, [pdf, pdf.with_suffix(".mpos")], None)
        return chord_heights(pdf)[0][0]

    assert seated(True) > seated(False) + 1.0  # the chords clear the slur, never print through it
