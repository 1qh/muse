"""Reduce a score to the pianist's sheet: 1 staff, notes only where the piano plays them, chords everywhere."""

from __future__ import annotations

import dataclasses
import itertools
import re

from muse.model import Bar, Event, Score, Text

SUNG = re.compile(r"\s*(Verse|Pre-?\s?Chorus|Chorus|Rap|Bridge)[\w-]*\s*:", re.IGNORECASE)
ENDING = re.compile(r"\s*(End|Ending|Outro)\w*\s*:", re.IGNORECASE)
LETTER = re.compile(r"^[A-Z]\d?$")
PIANO_CUE = re.compile(r"piano|keys?\b|key/|rhodes", re.IGNORECASE)
MIN_TAIL = 3
HINT_WORDS = 4
MIN_LINES = 2
SECOND_STAFF = 2


@dataclasses.dataclass
class Part:
    name: str
    first: int
    last: int
    staff: int = 1


HIT_CUE = re.compile(r"fill|tut+i", re.IGNORECASE)


def label_of(bar: Bar) -> str:
    """Find the lyric label a bar opens, such as Chorus-a, or an empty string.

    Returns:
        The label, or an empty string.
    """
    for text in [*bar.texts, *bar.texts2]:
        whole = "".join(run[0] for run in text.runs)
        if SUNG.match(whole) or ENDING.match(whole):
            return whole.split(":")[0].strip()
    return ""


def lyric_of(bar: Bar) -> str:
    """Find the hint words after a lyric label, so an ending can find the section it repeats.

    Returns:
        The hint words, or an empty string.
    """
    for text in [*bar.texts, *bar.texts2]:
        whole = "".join(run[0] for run in text.runs)
        if SUNG.match(whole) or ENDING.match(whole):
            return whole.split(":", 1)[1].strip().rstrip(".… ")
    return ""


def has_notes(bar: Bar) -> bool:
    return any(e.heads for voices in (bar.voices, bar.voices2) for v in voices for e in v)


def by_number_of(bars: list[Bar], number: int) -> Bar:
    return bars[number - 1]


def piano_cued(bar: Bar) -> bool:
    return any(PIANO_CUE.search("".join(run[0] for run in t.runs)) for t in [*bar.texts, *bar.texts2])


def segments(score: Score) -> list[tuple[int, int]]:
    """1-based [first, end) stretches between section marks, lyric labels and jumps.

    Returns:
        The stretches in bar order.
    """
    bars = score.bars
    cuts = {1, len(bars) + 1}
    for bar in bars:
        if bar.rehearsal or label_of(bar) or bar.coda:
            cuts.add(bar.number)
        if bar.dal_segno or bar.final:
            cuts.add(bar.number + 1)
        if bar.repeat_end and bar.number < len(bars) and piano_cued(by_number_of(bars, bar.number + 1)):
            cuts.add(bar.number + 1)
    ordered = sorted(c for c in cuts if c <= len(bars) + 1)
    return list(itertools.pairwise(ordered))


def sung_stretches(score: Score) -> list[tuple[int, int]]:
    """1-based [first, end) stretches a singer carries; a closing bar run past a sibling's length is instrumental.

    Returns:
        The sung stretches in bar order.
    """
    by_number = {b.number: b for b in score.bars}
    labelled = any(label_of(b) for b in score.bars)
    done: dict[str, int] = {}
    sung: list[tuple[int, int]] = []
    for first, end in segments(score):
        bar = by_number[first]
        label = label_of(bar)
        lettered = any(LETTER.match(mark) for mark in bar.rehearsal)
        if label and ENDING.match(label + ":"):
            _append_ending(score, bar, (first, end), sung)
            continue
        if label:
            closing = end > len(score.bars) or by_number[end - 1].dal_segno
            stop = min(end, first + done[label]) if label in done else end
            if label not in done and closing and sung:
                stop = min(end, first + list(done.values())[-1]) if done else end
            done.setdefault(label, end - first)
            sung.append((first, stop))
        elif bar.coda and end <= len(score.bars):
            sung.append((first, end))
        elif bar.coda:
            run = _tail_run(by_number, first, end)
            sung.append((first, end - run if run >= MIN_TAIL else end))
        elif lettered and not labelled:
            stop = end
            run = _tail_run(by_number, first, end)
            if run >= MIN_TAIL:
                stop = end - run
            sung.append((first, stop))
    return sung


def _append_ending(score: Score, bar: Bar, stretch: tuple[int, int], sung: list[tuple[int, int]]) -> None:
    """Add an ending's stretch, as long as the sung section it repeats."""
    first, end = stretch
    text = lyric_of(bar)
    twin = next((b for b in score.bars if b.number != first and lyric_of(b) and lyric_of(b) == text), None)
    if twin is not None:
        length = next(z - a for a, z in sung if a == twin.number) if any(a == twin.number for a, _ in sung) else 0
        sung.append((first, min(end, first + length)))


def _tail_run(by_number: dict[int, Bar], first: int, end: int) -> int:
    """Count the bars with notes closing a stretch, stopping at its first bar.

    Returns:
        The number of trailing bars with notes.
    """
    run = 0
    for number in range(end - 1, first, -1):
        if not has_notes(by_number[number]):
            break
        run += 1
    return run


def parts(score: Score) -> list[Part]:
    """Every stretch of bars that keeps its notes, named by where it falls.

    Returns:
        The kept parts in bar order.
    """
    sung = sung_stretches(score)
    kept = sorted(b.number for b in score.bars if not any(a <= b.number < z for a, z in sung))
    first_sung = sung[0][0] if sung else len(score.bars) + 1
    found: list[Part] = []
    for number in kept:
        if found and found[-1].last == number - 1 and not by_mark(score, number):
            found[-1].last = number
            continue
        name = "intro" if number < first_sung else "interlude"
        found.append(Part(name, number, number))
    found = [part for part in found if part.last > part.first or part.last == len(score.bars) or part.name == "intro"]
    if found and found[-1].last == len(score.bars) and found[-1].name != "intro":
        found[-1].name = "outro"
    for part in found:
        marked = any(mark.lower() == "interlude" for mark in score.bars[part.first - 1].rehearsal)
        if marked:
            part.name = "interlude"
    return found


def by_mark(score: Score, number: int) -> bool:
    return bool(score.bars[number - 1].rehearsal) or bool(score.bars[number - 1].coda)


def lone_lines(starts: list[int], total: int) -> list[int]:
    """Lines holding 1 bar, by bar number.

    Returns:
        The first bar number of each line holding 1 bar.
    """
    if len(starts) < MIN_LINES:
        return []
    return [first for first, stop in zip(starts, [*starts[1:], total + 1], strict=True) if stop - first == 1]


def voices_sound(voices: list[list[Event]]) -> bool:
    return any(e.heads for v in voices for e in v)


def staff_of(bars: list[Bar]) -> int:
    """Pick the staff cued piano or keys, otherwise the top; a top staff with no notes gives way to the one that has them.

    Returns:
        The staff number, 1 or 2.
    """
    for bar in bars:
        if any(PIANO_CUE.search("".join(run[0] for run in t.runs)) for t in bar.texts2):
            return 2
    if any(any(e.heads for v in b.voices2 for e in v) for b in bars) and not any(
        any(e.heads for v in b.voices for e in v) for b in bars
    ):
        return 2
    return 1


def empty_bar(bar: Bar) -> None:
    bar.voices = [[Event(bar.left, [], bar.length, bar_rest=True, hidden=True)]]


def shorten(text: Text) -> None:
    """Keep a hint lyric's label and first words."""
    whole = "".join(run[0] for run in text.runs)
    if not (SUNG.match(whole) or ENDING.match(whole)):
        return
    left = HINT_WORDS
    runs: list[tuple[str, bool, bool]] = []
    for content, bold, italic in text.runs:
        if ":" in content:
            runs.append((content, bold, italic))
            left = HINT_WORDS
            continue
        if bold:
            runs.append((content, bold, italic))
            continue
        words = content.split()
        if left <= 0:
            continue
        taken = words[:left]
        left -= len(taken)
        cut = left <= 0 and len(words) >= len(taken)
        runs.append((" " + " ".join(taken).rstrip(".…") + ("..." if cut else ""), bold, italic))
    text.runs = runs


def clean_ties(score: Score) -> None:
    """Drop a tie or slur whose other end was removed, since it would dangle."""
    events = [(e, h) for b in score.bars for v in b.voices for e in v for h in e.heads]
    open_ties = False
    for index, (_, head) in enumerate(events):
        following = events[index + 1][1] if index + 1 < len(events) else None
        if head.tie_stop and not open_ties:
            head.tie_stop = False
        if head.tie_start and not (
            following and following.tie_stop and following.step == head.step and following.octave == head.octave
        ):
            head.tie_start = False
        open_ties = head.tie_start
    opened = None
    for _, head in events:
        if head.slur_stop and opened is None:
            head.slur_stop = False
        elif head.slur_stop:
            opened = None
        if head.slur_start:
            if opened is not None:
                opened.slur_start = False
            opened = head
    if opened is not None:
        opened.slur_start = False


def hit_bars(score: Score) -> set[int]:
    """Bars the pianist plays with: any accent, and a fill or tutti cue through the bars until the next chord change.

    Returns:
        The bar numbers the pianist plays with.
    """
    found = {
        bar.number
        for bar in score.bars
        if any(event.accent for voices in (bar.voices, bar.voices2) for voice in voices for event in voice)
    }
    for at, bar in enumerate(score.bars):
        if any(HIT_CUE.search("".join(run[0] for run in text.runs)) for text in [*bar.texts, *bar.texts2]):
            found.add(bar.number)
            for later in score.bars[at + 1 :]:
                if later.chords or later.rehearsal or later.final:
                    break
                found.add(later.number)
    return found


def kept_parts(score: Score) -> list[Part]:
    """Collect the intro, interlude and outro, plus every run of hit bars outside them.

    Returns:
        The kept parts sorted by first bar.
    """
    found = parts(score)
    inside = {n for part in found for n in range(part.first, part.last + 1)}
    extra: list[Part] = []
    for number in sorted(hit_bars(score) - inside):
        if extra and extra[-1].last == number - 1:
            extra[-1].last = number
        else:
            extra.append(Part("hit", number, number))
    return sorted([*found, *extra], key=lambda part: part.first)


def reduce(score: Score) -> list[Part]:
    """Turn a score into the piano sheet in place, and name what kept its notes.

    Returns:
        The parts that kept their notes.
    """
    found = kept_parts(score)
    keep = {n: part for part in found for n in range(part.first, part.last + 1)}
    for part in found:
        part.staff = staff_of([score.bars[n - 1] for n in range(part.first, part.last + 1)])
    for bar in score.bars:
        held = keep.get(bar.number)
        if held is None:
            empty_bar(bar)
        else:
            first, second = (bar.voices2, bar.voices) if held.staff == SECOND_STAFF else (bar.voices, bar.voices2)
            bar.voices = first if voices_sound(first) else second if voices_sound(second) else first
            if not bar.voices:
                bar.voices = [[Event(bar.left, [], bar.length, bar_rest=True, hidden=True)]]
        bar.voices2 = []
        bar.texts = [*bar.texts, *bar.texts2]
        bar.texts2 = []
        for text in bar.texts:
            shorten(text)
        if held is not None and not any(e.heads for v in bar.voices for e in v):
            empty_bar(bar)
    score.has_second_staff = False
    clean_ties(score)
    return found
