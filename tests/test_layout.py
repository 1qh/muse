"""The calculated layout's own contracts."""
import pathlib
import types

import pytest

from muse import layout, port


def test_seated_render_refuses_a_missing_score(tmp_path: pathlib.Path) -> None:
    sheet = types.SimpleNamespace(path=lambda label: tmp_path / f"s-{label}.mscz")
    with pytest.raises(port.DoesNotFit, match="edits would be lost"):
        layout.render(sheet, "final", seated=True)  # type: ignore[arg-type]


def test_clear_gap_is_the_chord_gap_in_staff_spaces() -> None:
    chord_points = port.CHORD_SIZE_PER_SPACE  # at a spatium of 1 mm
    space_points = 72 / 25.4
    assert pytest.approx(port.CHORD_GAP * chord_points / space_points) == layout.CLEAR_SPACES


from fractions import Fraction as F  # noqa: E402

from muse.model import Bar, Chord, Event, Head, Score, Text  # noqa: E402

ROOM = 100.0  # a line's room; a plan fills 93 of it


def chord() -> Chord:
    return Chord(0.0, "C", 0, "")


def noted(number: int, tie: bool = False, chords: bool = True, **kw: object) -> Bar:
    head = Head(1.0, 0.0, "C", 5, tie_start=tie)
    return Bar(number, 0, 0.0, 100.0, F(4), voices=[[Event(1.0, [head], F(4))]], chords=[chord()] if chords else [], **kw)  # type: ignore[arg-type]


def empty(number: int, **kw: object) -> Bar:
    return Bar(number, 0, 0.0, 100.0, F(4), voices=[[Event(0.0, [], F(4), bar_rest=True)]], chords=[chord()], **kw)  # type: ignore[arg-type]


def lyric(label: str) -> Text:
    return Text(0.0, 0.0, [(label + ": ", True, True), ("la la", False, True)], False)


def lines(starts: set[int], total: int) -> list[int]:
    ordered = sorted(starts)
    return [stop - first for first, stop in zip(ordered, [*ordered[1:], total + 1], strict=True)]


def test_plan_spreads_bars_evenly_rather_than_packing() -> None:
    score = Score([noted(n) for n in range(1, 11)], [], [])
    assert lines(layout.plan(score, [18.0] * 10, ROOM), 10) == [5, 5]
    score = Score([noted(n) for n in range(1, 10)], [], [])
    assert sorted(lines(layout.plan(score, [18.0] * 9, ROOM), 9)) == [4, 5]


def test_plan_never_strands_a_lone_bar_when_as_many_lines_can_avoid_it() -> None:
    score = Score([noted(n) for n in range(1, 8)], [], [])
    assert 1 not in lines(layout.plan(score, [30.0] * 7, ROOM), 7)  # 3 per line fits; 3+3+1 strands 1, 3+2+2 or 2+3+2 does not


def test_plan_never_breaks_after_a_tied_bar() -> None:
    score = Score([noted(1), noted(2), noted(3, tie=True), noted(4), noted(5), noted(6)], [], [])
    assert 4 not in layout.plan(score, [30.0] * 6, ROOM)


def test_plan_never_opens_a_line_on_a_chordless_bar() -> None:
    score = Score([noted(1), noted(2), noted(3), noted(4, chords=False), noted(5), noted(6)], [], [])
    assert 4 not in layout.plan(score, [30.0] * 6, ROOM)


def test_plan_starts_a_line_at_every_section_mark() -> None:
    score = Score([noted(1), noted(2), noted(3, rehearsal=["A"]), noted(4)], [], [])
    assert 3 in layout.plan(score, [10.0] * 4, ROOM)


def test_piano_plan_lets_a_label_sit_second_after_a_lone_mark_bar() -> None:
    bars = [noted(1), empty(2, rehearsal=["A"]), empty(3, texts=[lyric("Verse1")]), empty(4)]
    score = Score(bars, [], [])
    assert 3 in layout.plan(score, [10.0] * 4, ROOM)  # band sheet: the label opens its own line
    assert 3 not in layout.plan(score, [10.0] * 4, ROOM, port.PIANO)


def test_plan_evens_empty_bars_in_its_width_estimate() -> None:
    score = Score([empty(n) for n in range(1, 5)], [], [])
    widths = [10.0, 10.0, 10.0, 40.0]  # evened, 4 empty bars need 160, so they cannot share 1 line
    assert len(layout.plan(score, widths, ROOM)) == 2


def test_plan_prefers_the_even_split_of_as_many_lines() -> None:
    score = Score([noted(n) for n in range(1, 9)], [], [])
    assert lines(layout.plan(score, [12.0] * 8, ROOM), 8) == [4, 4]  # 7 fit on a line; 5+3 is as legal and as short
