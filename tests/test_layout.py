"""The calculated layout's own contracts."""

import os
import pathlib
import types
import zipfile
from dataclasses import dataclass
from fractions import Fraction
from typing import TypeGuard, override

import pytest

from muse import chordseat, layout, port
from muse.model import Bar, Chord, Event, Head, Score, Text

ROOM = 100.0


def _is_string_list(value: object) -> TypeGuard[list[str]]:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _is_text_list(value: object) -> TypeGuard[list[Text]]:
    return isinstance(value, list) and all(isinstance(item, Text) for item in value)


class _StandInSheet(layout.Sheet):
    """Stand in for a sheet with a given score, a folder its scores sit in and the bars that open its lines."""

    def __init__(self, score: Score, folder: pathlib.Path, opening: list[int]) -> None:
        self.score = score
        self.folder = folder
        self.opening = opening
        self.shares = []

    @override
    def path(self, label: str) -> pathlib.Path:
        return self.folder / f"s-{label}.mscz"

    @override
    def starts(self) -> list[int]:
        return list(self.opening)


def test_seated_render_refuses_a_missing_score(tmp_path: pathlib.Path) -> None:
    sheet = _StandInSheet(Score([], [], []), tmp_path, [1])
    with pytest.raises(port.DoesNotFitError, match="edits would be lost"):
        layout.render(sheet, "final", seated=True)


def test_clear_gap_is_the_chord_gap_in_staff_spaces() -> None:
    chord_points = port.CHORD_SIZE_PER_SPACE
    space_points = 72 / 25.4
    assert pytest.approx(port.CHORD_GAP * chord_points / space_points) == layout.CLEAR_SPACES


def chord() -> Chord:
    return Chord(0.0, "C", 0, "")


def noted(number: int, **fields: object) -> Bar:
    allowed = {"tie", "chords", "rehearsal", "texts", "new_system"}
    unknown = next((name for name in fields if name not in allowed), None)
    if unknown is not None:
        msg = f"noted() got an unexpected keyword argument '{unknown}'"
        raise TypeError(msg)
    tie = fields.get("tie", False)
    chords = fields.get("chords", True)
    rehearsal_value = fields.get("rehearsal")
    texts_value = fields.get("texts")
    new_system = fields.get("new_system", False)
    if not isinstance(tie, bool) or not isinstance(chords, bool) or not isinstance(new_system, bool):
        msg = "noted boolean fields must be bool"
        raise TypeError(msg)
    if rehearsal_value is None:
        rehearsal_value = []
    if texts_value is None:
        texts_value = []
    if not _is_string_list(rehearsal_value):
        msg = "noted rehearsal must be a list of strings"
        raise TypeError(msg)
    if not _is_text_list(texts_value):
        msg = "noted texts must be a list of Text"
        raise TypeError(msg)
    head = Head(1.0, 0.0, "C", 5, tie_start=tie)
    return Bar(
        number,
        0,
        0.0,
        100.0,
        Fraction(4),
        voices=[[Event(1.0, [head], Fraction(4))]],
        chords=[chord()] if chords else [],
        rehearsal=rehearsal_value or [],
        texts=texts_value or [],
        new_system=new_system,
    )


def empty(number: int, *, rehearsal: list[str] | None = None, texts: list[Text] | None = None) -> Bar:
    return Bar(
        number,
        0,
        0.0,
        100.0,
        Fraction(4),
        voices=[[Event(0.0, [], Fraction(4), bar_rest=True)]],
        chords=[chord()],
        rehearsal=rehearsal or [],
        texts=texts or [],
    )


def lyric(label: str) -> Text:
    return Text(0.0, 0.0, [(label + ": ", True, True), ("la la", False, True)], above=False)


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
    assert 1 not in lines(layout.plan(score, [30.0] * 7, ROOM), 7)


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
    bars = [noted(1), noted(2), empty(3, rehearsal=["A"]), empty(4, texts=[lyric("Verse1")]), empty(5)]
    score = Score(bars, [], [])
    assert 4 not in layout.plan(score, [10.0] * 5, ROOM)
    assert 4 not in layout.plan(score, [10.0] * 5, ROOM, port.PIANO)


def test_piano_plan_leaves_no_section_of_1_bar_alone_on_a_line() -> None:
    bars = [noted(1), empty(2, rehearsal=["A"]), empty(3, texts=[lyric("Verse1")]), empty(4)]
    score = Score(bars, [], [])
    starts = sorted(layout.plan(score, [10.0] * 4, ROOM, port.PIANO))
    lengths = [b - a for a, b in zip(starts, [*starts[1:], 5], strict=False)]
    assert 1 not in lengths


def test_plan_evens_empty_bars_in_its_width_estimate() -> None:
    score = Score([empty(n) for n in range(1, 5)], [], [])
    widths = [10.0, 10.0, 10.0, 40.0]
    assert len(layout.plan(score, widths, ROOM)) == 2


def test_plan_prefers_the_even_split_of_as_many_lines() -> None:
    score = Score([noted(n) for n in range(1, 9)], [], [])
    assert lines(layout.plan(score, [12.0] * 8, ROOM), 8) == [4, 4]


@dataclass(frozen=True)
class CandidateOptions:
    held: bool = False
    share: float = 0.0
    fill: float = 0.9
    good: bool = True
    lines: int = 10
    lone: bool = False


def candidate(size: float, options: CandidateOptions = CandidateOptions()) -> layout.Candidate:
    return {
        "size": size,
        "held": options.held,
        "share": options.share,
        "fill": options.fill,
        "good": options.good,
        "lines": options.lines,
        "lone": options.lone,
    }


@pytest.fixture
def strict_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the choice rules at their strict values, whatever mode the run is in.

    These tests pin the rules, not the mode.
    """
    monkeypatch.setattr(port, "PAGE_FILL", 0.85)
    monkeypatch.setattr(port, "MAX_SHORT_SHARE", 0.15)
    monkeypatch.setattr(port, "RELAXED_SHORT_SHARE", 0.30)


@pytest.mark.usefixtures("strict_choice")
def test_choose_takes_the_largest_size_that_fills_its_pages_first() -> None:
    found = [candidate(2.0, CandidateOptions(fill=0.5)), candidate(1.8), candidate(1.6, CandidateOptions(fill=0.95))]
    assert layout.choose(found, port.ORIGINAL)[:2] == [1.8, 1.6]


@pytest.mark.usefixtures("strict_choice")
def test_choose_takes_the_wider_short_line_share_only_when_no_size_holds_the_base_share() -> None:
    found = [candidate(2.0, CandidateOptions(share=0.25)), candidate(1.6, CandidateOptions(share=0.10))]
    assert layout.choose(found, port.ORIGINAL)[0] == pytest.approx(1.6)
    found = [candidate(2.0, CandidateOptions(share=0.25)), candidate(1.6, CandidateOptions(share=0.28))]
    assert layout.choose(found, port.ORIGINAL)[0] == pytest.approx(2.0)
    assert layout.choose([candidate(2.0, CandidateOptions(share=0.4))], port.ORIGINAL) == []


def test_choose_refuses_a_size_whose_lines_did_not_hold_or_leave_a_lone_piano_bar() -> None:
    assert layout.choose([candidate(2.0, CandidateOptions(good=False)), candidate(1.5)], port.ORIGINAL) == [1.5]
    assert layout.choose([candidate(2.0, CandidateOptions(lone=True)), candidate(1.5)], port.PIANO) == [1.5]


def test_plan_lets_a_short_line_take_a_bar_from_the_line_above() -> None:
    score = Score([noted(n) for n in range(1, 8)], [], [])
    assert 2 not in lines(layout.plan(score, [18.0] * 7, ROOM), 7)


def test_plan_takes_a_bar_from_the_line_above_rather_than_leave_a_2_bar_line() -> None:
    score = Score([noted(n) for n in range(1, 7)], [], [])
    assert lines(layout.plan(score, [12.0, 18.0, 8.0, 45.0, 30.0, 12.0], ROOM), 6) == [3, 3]


def test_space_points_reads_the_scores_own_staff_space(tmp_path: pathlib.Path) -> None:
    score = tmp_path / "s.mscz"
    with zipfile.ZipFile(score, "w") as archive:
        archive.writestr("s.mscx", "<museScore><Score/></museScore>")
        archive.writestr("score_style.mss", "<museScore><Style><spatium>1.6</spatium></Style></museScore>")
    assert chordseat.space_points(score) == pytest.approx(1.6 * 72 / 25.4)


@pytest.mark.usefixtures("strict_choice")
def test_choose_packs_a_piano_sheet_into_the_fewest_lines_then_the_largest_notes() -> None:
    found = [
        candidate(2.0, CandidateOptions(lines=30)),
        candidate(1.6, CandidateOptions(lines=22)),
        candidate(1.5, CandidateOptions(lines=20)),
        candidate(1.4, CandidateOptions(lines=20)),
    ]
    assert layout.choose(found, port.PIANO)[0] == pytest.approx(1.5)
    found = [candidate(1.5, CandidateOptions(lines=20, share=0.4)), candidate(1.8, CandidateOptions(lines=24))]
    assert layout.choose(found, port.PIANO)[0] == pytest.approx(1.8)


def test_every_size_of_a_sheet_shares_1_import(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    score = Score([noted(1)], [], [])
    monkeypatch.setattr(layout, "decode", lambda *_, **__: score)
    monkeypatch.setattr(layout, "song_of", lambda *_, **__: types.SimpleNamespace(title="T", name="Song"))
    monkeypatch.setattr(layout.port, "transpose_options", lambda *_, **__: None)
    styles = []
    monkeypatch.setattr(
        layout.port,
        "imported",
        lambda _decoded, work, _name, style, **_options: styles.append(style.read_text()) or work / "import.mscz",
    )
    for size in (1.3, 1.8):
        layout.Sheet(tmp_path / "Song-Em.pdf", tmp_path, port.ORIGINAL, size).imported()
    assert len(styles) == 2
    assert styles[0] == styles[1]


def test_finish_widens_the_line_gap_while_text_meets_text(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    tried = []

    def finish_at(*args: *tuple[object, object, object, port.Sizing, object]) -> tuple[object, ...]:
        _src, _work, _variant, (size, distance), _learned = args
        tried.append(distance)
        if len(tried) < 3:
            msg = "s at 2.0: ['text on text: B']"
            raise port.DoesNotFitError(msg)
        return ("score", "path", size, distance, 1)

    monkeypatch.setattr(layout, "finish_at", finish_at)
    assert layout.finish(tmp_path, tmp_path, port.ORIGINAL, 2.0)[3] == port.BASE_DISTANCE + 2 * layout.GAP_STEP
    monkeypatch.setattr(layout, "finish_at", lambda *_: (_ for _ in ()).throw(port.DoesNotFitError("lines still wrap")))
    with pytest.raises(port.DoesNotFitError, match="wrap"):
        layout.finish(tmp_path, tmp_path, port.ORIGINAL, 2.0)


def test_a_label_may_sit_second_after_a_mark_bar_opening_its_line_on_any_sheet() -> None:
    bars = [noted(1), noted(2), empty(3, rehearsal=["A"]), empty(4, texts=[lyric("Chorus")]), empty(5)]
    score = Score(bars, [], [])
    for bar in score.bars:
        bar.new_system = bar.number == 3
    assert port.line_rule_breaks(score, port.PIANO) == []
    assert port.line_rule_breaks(score, port.ORIGINAL) == []


def test_plan_breaks_a_soft_rule_rather_than_overflow_a_line() -> None:
    bars = [noted(1)] + [noted(n, chords=False) for n in range(2, 8)]
    score = Score(bars, [], [])
    starts = layout.plan(score, [30.0] * 7, ROOM)
    assert len(starts) > 1


def test_plan_never_strands_a_bar_its_grown_estimate_makes_wide() -> None:
    score = Score([noted(n) for n in range(1, 6)], [], [])
    assert 1 not in lines(layout.plan(score, [55.0, 20.0, 20.0, 20.0, 20.0], ROOM), 5)


def test_every_line_gap_retry_starts_from_the_widths_the_last_one_learned(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    seen: list[object] = []

    def finish_at(*args: *tuple[object, object, object, port.Sizing, list[list[float]] | None]) -> tuple[object, ...]:
        _src, _work, _variant, (size, distance), learned = args
        seen.append(learned)
        assert learned is not None
        learned.append([float(len(seen))])
        if len(seen) < 3:
            msg = "s at 2.0: ['text on text: B']"
            raise port.DoesNotFitError(msg)
        return ("score", "path", size, distance, 1)

    monkeypatch.setattr(layout, "finish_at", finish_at)
    layout.finish(tmp_path, tmp_path, port.ORIGINAL, 2.0)
    assert len(seen) == 3
    assert all(record is seen[0] for record in seen)


def test_a_measured_gap_past_the_ink_limit_never_feeds_the_correction() -> None:
    assert layout.next_bias([0.1, 0.2], [0.9, 16.0 * 4.5], [4.5, 4.5]) == [0.1 + 0.2, 0.2]
    assert layout.next_bias([], [None, -4.5], [4.5, 4.5]) == [0.0, -1.0]


def test_a_line_printing_fewer_chords_than_the_score_holds_is_crowded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    def bar(number: int, names: int, *, new: bool) -> Bar:
        made = Bar(number, 0, 0.0, 100.0, Fraction(4), chords=[Chord(float(at), "C", 0, "") for at in range(names)])
        made.new_system = new
        return made

    score = Score([bar(1, 2, new=False), bar(2, 1, new=False), bar(3, 2, new=True), bar(4, 2, new=False)], [], [])
    monkeypatch.setattr(
        chordseat,
        "page_lines",
        lambda *_, **__: [{"chords": [1.0, 2.0, 3.0]}, {"chords": [1.0, 2.0, 3.0]}],
    )
    assert layout.merged_rows(score, tmp_path / "p.pdf") == {3}


def test_the_seated_overflow_check_reports_lines_whose_chords_merged(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    bars = [Bar(n, 0, 0.0, 100.0, Fraction(4)) for n in range(1, 5)]
    bars[2].new_system = True
    sheet = _StandInSheet(Score(bars, [], []), tmp_path, [1, 3])
    monkeypatch.setattr(port, "system_starts", lambda *_, **__: [1, 3])
    monkeypatch.setattr(port, "crowded_rows", lambda *_, **__: set())
    monkeypatch.setattr(layout, "merged_rows", lambda *_, **__: {3})
    assert layout.overflowing(sheet, tmp_path / "p.pdf", seated=True) == {2, 3}


def test_a_1_bar_section_shares_its_line_with_the_next_section_whatever_opens_it() -> None:
    bars = [Bar(n, 0, 0.0, 100.0, Fraction(4)) for n in range(1, 6)]
    bars[2].rehearsal = ["A1"]
    bars[3].rehearsal = ["A2"]
    score = Score(bars, [], [])
    assert layout.blocks_of(score, port.PIANO) == [[0, 1], [2, 3, 4]]
    assert layout.blocks_of(score, port.ORIGINAL) == [[0, 1], [2, 3, 4]]


def test_the_seating_rounds_count_gaps_in_the_scores_own_staff_space(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    tags: list[dict[str, str]] = []
    monkeypatch.setattr(layout, "set_tags", lambda _path, values: tags.append(dict(values)))
    monkeypatch.setattr(layout, "run_extension", lambda *_, **__: None)
    monkeypatch.setattr(chordseat, "score_text", lambda *_, **__: ("s.mscx", ""))
    monkeypatch.setattr(chordseat, "rows_of", lambda *_, **__: [[(0, True)]])
    monkeypatch.setattr(
        chordseat,
        "matched",
        lambda *_, **__: [{"chords": [50.0], "lefts": [50.0], "rights": [60.0], "space": 5.0}],
    )
    monkeypatch.setattr(chordseat, "gaps", lambda *_, **__: [3.0])
    monkeypatch.setattr(chordseat, "space_points", lambda *_, **__: 4.5)
    monkeypatch.setattr(layout.pdfcheck, "chord_collisions", lambda *_, **__: [])
    monkeypatch.setattr(layout.pdfcheck, "sign_word_overlaps", lambda *_, **__: [])
    monkeypatch.setattr(layout.chordseat, "jump_crossings", lambda *_, **__: [])
    (tmp_path / "s.mscz").write_text("score")
    (tmp_path / "s.pdf").write_text("render")
    layout.seat_inked(tmp_path / "s.mscz", lambda: tmp_path / "s.pdf", lambda *_, **__: True)
    second = next(values for values in tags[1:] if "museInkBias" in values)
    assert second["museInkBias"] == f"{3.0 / 4.5:.4f}"


def test_a_line_of_1_bar_breaks_the_line_rules_on_any_sheet() -> None:
    bars = [noted(n) for n in range(1, 6)]
    score = Score(bars, [], [])
    for bar in score.bars:
        bar.new_system = bar.number in {3, 4}
    assert port.line_rule_breaks(score, port.ORIGINAL) == [3]
    assert port.line_rule_breaks(score, port.PIANO) == [3]


def test_the_line_gap_widens_past_any_fixed_count_until_the_checks_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    tried: list[float] = []

    def finish_at(*args: *tuple[object, object, object, port.Sizing, object]) -> tuple[object, ...]:
        _src, _work, _variant, (size, distance), _learned = args
        tried.append(distance)
        if distance < port.BASE_DISTANCE + 12:
            msg = "text on text: a cue below sits too close"
            raise port.DoesNotFitError(msg)
        return ("score", "path", size, distance, 1)

    monkeypatch.setattr(layout, "finish_at", finish_at)
    assert layout.finish(pathlib.Path("s.pdf"), pathlib.Path("w"), "original", 2.0)[3] == port.BASE_DISTANCE + 12
    assert len(tried) == 7


def test_a_line_may_open_on_a_chordless_bar_only_where_the_source_opens_one() -> None:
    score = Score([noted(1), noted(2), noted(3, chords=False), noted(4), noted(5)], [], [])
    widths = [30.0, 30.0, 30.0, 28.0, 28.0]
    assert 3 not in layout.plan(score, widths, ROOM)
    score.bars[2].source_line = True
    assert 3 in layout.plan(score, widths, ROOM)
    for bar, first in zip(score.bars, (True, False, True, False, False), strict=True):
        bar.new_system = first
    assert port.line_rule_breaks(score, port.ORIGINAL) == []
    score.bars[2].source_line = False
    assert port.line_rule_breaks(score, port.ORIGINAL) == [3]


def test_the_layout_refuses_a_line_break_inside_a_short_tie() -> None:
    score = Score([noted(1), noted(2), noted(3, tie=True), noted(4), noted(5)], [], [])
    for bar, first in zip(score.bars, (True, False, False, True, False), strict=True):
        bar.new_system = first
    assert port.line_rule_reasons(score) == ["tie over line end into bar 4 (tied run of 2 bars)"]


def test_the_layout_judges_even_empty_bars_in_strict_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    monkeypatch.setattr(layout.mode, "PRINT", False)
    seen = []
    monkeypatch.setattr(
        layout.chordseat,
        "uneven",
        lambda _mscz, _pdf, noted: seen.append(noted) or ["bar 2: 30.0 pt wide, line mean 20.0"],
    )
    sheet = _StandInSheet(Score([noted(1), empty(2)], [], []), tmp_path, [1])
    assert layout.uneven_empty_bars(sheet, tmp_path / "s.mscz", tmp_path / "s.pdf") == [
        "bar 2: 30.0 pt wide, line mean 20.0",
    ]
    assert seen == [{1}]


def test_a_lines_empty_bars_are_evened_to_the_widest(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """An empty bar widens for its own chords, and the rest of its line's empty bars follow it, so they stay equal."""
    set_to: dict[int, float] = {}
    monkeypatch.setattr(layout.chordseat, "score_text", lambda *_, **__: ("s", ""))
    monkeypatch.setattr(layout.chordseat, "matched", lambda *_, **__: [])
    monkeypatch.setattr(layout.chordseat, "empty_lines", lambda *_, **__: [[(2, 20.0), (3, 40.0), (4, 20.0)]])
    monkeypatch.setattr(layout.chordseat, "set_stretch", lambda _mscz, off: set_to.update(off))
    assert layout.chordseat.equalize(tmp_path / "s.mscz", tmp_path / "s.pdf", set())
    assert set_to == {2: 2.0, 4: 2.0}


def test_a_sheet_whose_pages_are_not_balanced_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """A line pushed onto a page of its own, as 10, 1 and 11 lines, is refused.

    The balanced split of the same lines passes.
    """
    score = Score([noted(n, new_system=True) for n in range(1, 23)], [], [])
    monkeypatch.setattr(port, "line_heights", lambda *_, **__: ([10.0] * 22, 3))
    balanced = [
        b - a
        for a, b in zip([0, *port.balanced_pages([10.0] * 22, 3)], [*port.balanced_pages([10.0] * 22, 3), 22], strict=True)
    ]
    monkeypatch.setattr(port, "lines_per_page", lambda *_, **__: [10, 1, 11])
    assert port.page_problems(score, tmp_path / "s.pdf") == [f"pages hold [10, 1, 11] lines, balanced {balanced}"]
    monkeypatch.setattr(port, "lines_per_page", lambda *_, **__: balanced)
    assert port.page_problems(score, tmp_path / "s.pdf") == []


def test_balancing_replaces_every_earlier_page_break(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    score = Score([noted(n, new_system=True) for n in range(1, 7)], [], [])
    score.bars[4].new_page = True
    monkeypatch.setattr(port, "line_heights", lambda *_, **__: ([10.0] * 6, 2))
    assert port.balance(score, tmp_path / "s.pdf")
    assert [bar.number for bar in score.bars if bar.new_page] == [4]


def test_every_sheet_takes_the_size_giving_the_fewest_lines() -> None:
    fit: list[layout.Candidate] = [
        candidate(1.8, CandidateOptions(lines=24)),
        candidate(1.3, CandidateOptions(lines=17, fill=0.7)),
        candidate(1.25, CandidateOptions(lines=17)),
        candidate(1.2, CandidateOptions(lines=17)),
    ]
    under_the_floor = [1.25, 1.2, 1.3, 1.8]
    without_a_floor = [1.3, 1.25, 1.2, 1.8]
    assert layout.ranked(fit) == (under_the_floor if port.PAGE_FILL > 0.7 else without_a_floor)
    assert layout.ranked([*fit, candidate(1.15, CandidateOptions(lines=15))])[-1] == pytest.approx(1.15)


def test_a_score_written_after_its_render_is_rendered_again(tmp_path: pathlib.Path) -> None:
    score, probe = tmp_path / "s.mscz", tmp_path / "s.pdf"
    probe.write_text("render")
    score.write_text("score")
    os.utime(probe, ns=(1_000_000_000, 1_000_000_000))
    os.utime(score, ns=(2_000_000_000, 2_000_000_000))
    fresh = tmp_path / "fresh.pdf"
    assert layout.current_render(score, probe, lambda: fresh) == fresh
    os.utime(probe, ns=(3_000_000_000, 3_000_000_000))
    assert layout.current_render(score, probe, lambda: fresh) == probe


def test_the_size_is_chosen_on_finished_sheets(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """The best plan can finish with more lines than a plan ranked after it; the finished counts decide."""

    def sheet_of(lines: int) -> Score:
        bars = [noted(n, new_system=n > 1) for n in range(1, lines + 1)]
        return Score(bars, [], [])

    planned = {1.5: 10, 1.3: 12, 1.2: 14}
    finished = {1.5: 16, 1.3: 13, 1.2: 15}
    monkeypatch.setattr(
        layout,
        "candidate",
        lambda _src, _work, _variant, size: {
            "size": size,
            "lines": planned.get(size, 99),
            "fill": 0.9,
            "held": True,
            "share": 0.0,
            "good": True,
            "lone": False,
        },
    )
    monkeypatch.setattr(layout, "choose", lambda *_, **__: [1.5, 1.3, 1.2])
    monkeypatch.setattr(
        layout,
        "finish",
        lambda _src, _work, _variant, size: (sheet_of(finished[size]), tmp_path / f"{size}.mscz", size, 1.0, 1),
    )
    assert layout.lay(tmp_path / "s.pdf", tmp_path, port.ORIGINAL)[2] == pytest.approx(1.3)


def test_a_finished_sheet_past_the_widest_short_line_share_is_refused() -> None:
    sparse = [[1, 2], [3, 4], [5, 6, 7, 8], [9, 10]]
    full = [[1, 2, 3, 4], [5, 6, 7, 8], [9, 10, 11, 12], [13, 14]]
    assert port.short_line_problems(sparse) != []
    assert port.short_line_problems(full) == []
