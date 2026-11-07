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


def candidate(size: float, share: float = 0.0, fill: float = 0.9, good: bool = True, lone: bool = False, lines: int = 10) -> dict[str, object]:
    return {"size": size, "share": share, "fill": fill, "good": good, "lone": lone, "lines": lines}


def test_choose_takes_the_largest_size_that_fills_its_pages_first() -> None:
    found = [candidate(2.0, fill=0.5), candidate(1.8, fill=0.9), candidate(1.6, fill=0.95)]
    assert layout.choose(found, port.ORIGINAL)[:2] == [1.8, 1.6]


def test_choose_takes_the_wider_short_line_share_only_when_no_size_holds_the_base_share() -> None:
    found = [candidate(2.0, share=0.25), candidate(1.6, share=0.10)]
    assert layout.choose(found, port.ORIGINAL)[0] == 1.6
    found = [candidate(2.0, share=0.25), candidate(1.6, share=0.28)]
    assert layout.choose(found, port.ORIGINAL)[0] == 2.0
    assert layout.choose([candidate(2.0, share=0.4)], port.ORIGINAL) == []


def test_choose_refuses_a_size_whose_lines_did_not_hold_or_leave_a_lone_piano_bar() -> None:
    assert layout.choose([candidate(2.0, good=False), candidate(1.5)], port.ORIGINAL) == [1.5]
    assert layout.choose([candidate(2.0, lone=True), candidate(1.5)], port.PIANO) == [1.5]


def test_plan_lets_a_short_line_take_a_bar_from_the_line_above() -> None:
    score = Score([noted(n) for n in range(1, 8)], [], [])
    assert 2 not in lines(layout.plan(score, [18.0] * 7, ROOM), 7)  # 5 fit; 5+2 leaves a 2-bar line where 4+3 leaves none


def test_plan_takes_a_bar_from_the_line_above_rather_than_leave_a_2_bar_line() -> None:
    score = Score([noted(n) for n in range(1, 7)], [], [])
    assert lines(layout.plan(score, [12.0, 18.0, 8.0, 45.0, 30.0, 12.0], ROOM), 6) == [3, 3]  # 4+2 fills a little more evenly


def test_space_points_reads_the_scores_own_staff_space(tmp_path: pathlib.Path) -> None:
    import zipfile

    from muse import chordseat

    score = tmp_path / "s.mscz"
    with zipfile.ZipFile(score, "w") as archive:
        archive.writestr("s.mscx", "<museScore><Score/></museScore>")
        archive.writestr("score_style.mss", "<museScore><Style><spatium>1.6</spatium></Style></museScore>")
    assert chordseat.space_points(score) == pytest.approx(1.6 * 72 / 25.4)


def test_choose_packs_a_piano_sheet_into_the_fewest_lines_then_the_largest_notes() -> None:
    found = [candidate(2.0, lines=30), candidate(1.6, lines=22), candidate(1.5, lines=20), candidate(1.4, lines=20)]
    assert layout.choose(found, port.PIANO)[0] == 1.5
    assert layout.choose([candidate(1.5, lines=20, share=0.4), candidate(1.8, lines=24)], port.PIANO)[0] == 1.8  # short lines still bound a piano sheet


def test_every_size_of_a_sheet_shares_1_import(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    score = Score([noted(1)], [], [])
    monkeypatch.setattr(layout, "decode", lambda src: score)
    monkeypatch.setattr(layout, "song_of", lambda src, decoded=None: types.SimpleNamespace(title="T", name="Song"))
    monkeypatch.setattr(layout.port, "transpose_options", lambda info: None)
    styles = []
    monkeypatch.setattr(layout.port, "imported", lambda decoded, work, name, style, clear: styles.append(style.read_text()) or work / "import.mscz")
    for size in (1.3, 1.8):
        layout.Sheet(tmp_path / "Song-Em.pdf", tmp_path, port.ORIGINAL, size).imported()
    assert len(styles) == 2 and styles[0] == styles[1]


def test_finish_widens_the_line_gap_while_text_meets_text(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    tried = []

    def finish_at(src: object, work: object, variant: object, size: float, distance: float, learned: object = None) -> tuple[object, ...]:
        tried.append(distance)
        if len(tried) < 3:
            raise port.DoesNotFit("s at 2.0: ['text on text: B']")
        return ("score", "path", size, distance, 1)

    monkeypatch.setattr(layout, "finish_at", finish_at)
    assert layout.finish(tmp_path, tmp_path, port.ORIGINAL, 2.0)[3] == port.BASE_DISTANCE + 2 * layout.GAP_STEP  # type: ignore[arg-type]
    monkeypatch.setattr(layout, "finish_at", lambda *a: (_ for _ in ()).throw(port.DoesNotFit("lines still wrap")))
    with pytest.raises(port.DoesNotFit, match="wrap"):
        layout.finish(tmp_path, tmp_path, port.ORIGINAL, 2.0)


def test_a_piano_label_may_sit_second_after_a_mark_bar_opening_its_line() -> None:
    bars = [noted(1), empty(2, rehearsal=["A"]), empty(3, texts=[lyric("Chorus")]), empty(4)]
    score = Score(bars, [], [])
    for bar in score.bars:
        bar.new_system = bar.number == 2
    assert port.line_rule_breaks(score, port.PIANO) == []
    assert port.line_rule_breaks(score, port.ORIGINAL) == [3]


def test_plan_breaks_a_soft_rule_rather_than_overflow_a_line() -> None:
    bars = [noted(1)] + [noted(n, chords=False) for n in range(2, 8)]  # only bar 1 has a chord, so every split opens a line without one
    score = Score(bars, [], [])
    starts = layout.plan(score, [30.0] * 7, ROOM)
    assert len(starts) > 1  # 7 bars of 30 cannot share 1 line of 93


def test_plan_never_strands_a_bar_its_grown_estimate_makes_wide() -> None:
    score = Score([noted(n) for n in range(1, 6)], [], [])
    assert 1 not in lines(layout.plan(score, [55.0, 20.0, 20.0, 20.0, 20.0], ROOM), 5)  # the wide first bar still takes a neighbour


def test_every_line_gap_retry_starts_from_the_widths_the_last_one_learned(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    seen: list[object] = []

    def finish_at(src: object, work: object, variant: object, size: float, distance: float, learned: list[list[float]] | None = None) -> tuple[object, ...]:
        seen.append(learned)
        assert learned is not None
        learned.append([float(len(seen))])
        if len(seen) < 3:
            raise port.DoesNotFit("s at 2.0: ['text on text: B']")
        return ("score", "path", size, distance, 1)

    monkeypatch.setattr(layout, "finish_at", finish_at)
    layout.finish(tmp_path, tmp_path, port.ORIGINAL, 2.0)  # type: ignore[arg-type]
    assert len(seen) == 3 and all(record is seen[0] for record in seen)


def test_a_measured_gap_past_the_ink_limit_never_feeds_the_correction() -> None:
    from muse import layout

    assert layout.next_bias([0.1, 0.2], [0.9, 16.0 * 4.5], [4.5, 4.5]) == [0.1 + 0.2, 0.2]
    assert layout.next_bias([], [None, -4.5], [4.5, 4.5]) == [0.0, -1.0]
