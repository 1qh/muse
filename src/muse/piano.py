"""Reduce a score to the pianist's sheet: 1 staff, notes only where the piano plays them, chords everywhere."""
from __future__ import annotations

import dataclasses
import re

from muse import chordnames
from muse.model import Bar, Event, Score, Text

SUNG = re.compile(r"\s*(Verse|Pre-?\s?Chorus|Chorus|Rap|Bridge)[\w-]*\s*:", re.I)
ENDING = re.compile(r"\s*(End|Ending|Outro)\w*\s*:", re.I)
LETTER = re.compile(r"^[A-Z]\d?$")
PIANO_CUE = re.compile(r"piano|keys?\b|key/|rhodes", re.I)
MIN_TAIL = 3  # consecutive bars of notes closing an unlabelled section read as instrumental
HINT_WORDS = 4
CHORD_GAP_LETTERS = 2


@dataclasses.dataclass
class Part:
    name: str  # intro, interlude or outro
    first: int
    last: int
    staff: int = 1


HIT_CUE = re.compile(r"fill|tut+i", re.I)


def label_of(bar: Bar) -> str:
    """The lyric label a bar opens, such as Chorus-a, or an empty string."""
    for text in [*bar.texts, *bar.texts2]:
        whole = "".join(run[0] for run in text.runs)
        if SUNG.match(whole) or ENDING.match(whole):
            return whole.split(":")[0].strip()
    return ""


def lyric_of(bar: Bar) -> str:
    """The hint words after a lyric label, so an ending can find the section it repeats."""
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
    """1-based [first, end) stretches between section marks, lyric labels and jumps."""
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
    return list(zip(ordered, ordered[1:], strict=False))


def sung_stretches(score: Score) -> list[tuple[int, int]]:
    """1-based [first, end) stretches a singer carries; a closing bar run past a sibling's length is instrumental."""
    by_number = {b.number: b for b in score.bars}
    labelled = any(label_of(b) for b in score.bars)
    done: dict[str, int] = {}
    sung: list[tuple[int, int]] = []
    for first, end in segments(score):
        bar = by_number[first]
        label = label_of(bar)
        lettered = any(LETTER.match(mark) for mark in bar.rehearsal)
        if label and ENDING.match(label + ":"):
            text = lyric_of(bar)
            twin = next((b for b in score.bars if b.number != first and lyric_of(b) and lyric_of(b) == text), None)
            if twin is not None:
                length = next(z - a for a, z in sung if a == twin.number) if any(a == twin.number for a, _ in sung) else 0
                sung.append((first, min(end, first + length)))
            continue
        if label:
            closing = end > len(score.bars) or by_number[end - 1].dal_segno
            stop = min(end, first + done[label]) if label in done else end
            if label not in done and closing and sung:  # a last section with no earlier twin runs as long as the one before
                stop = min(end, first + list(done.values())[-1]) if done else end
            done.setdefault(label, end - first)
            sung.append((first, stop))
        elif bar.coda and end <= len(score.bars):
            sung.append((first, end))
        elif bar.coda:
            run = 0
            for number in range(end - 1, first, -1):
                if not has_notes(by_number[number]):
                    break
                run += 1
            sung.append((first, end - run if run >= MIN_TAIL else end))
        elif lettered and not labelled:
            stop = end
            run = 0
            for number in range(end - 1, first, -1):
                if not has_notes(by_number[number]):
                    break
                run += 1
            if run >= MIN_TAIL:
                stop = end - run
            sung.append((first, stop))
    return sung


def parts(score: Score) -> list[Part]:
    """Every stretch of bars that keeps its notes, named by where it falls."""
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


def chord_letters(bar: Bar) -> int:
    """Room a bar's chord names ask for, in letters, with the gap each chord needs."""
    return sum(len(c.root) + bool(c.root_alter) + len(chordnames.stored(c.kind)) + (len(c.bass) + 1 if c.bass else 0) + CHORD_GAP_LETTERS for c in bar.chords)


def lone_lines(starts: list[int], total: int) -> list[int]:
    """Lines holding 1 bar, by bar number."""
    if len(starts) < 2:
        return []  # a single line has no neighbour to join
    return [first for first, stop in zip(starts, [*starts[1:], total + 1], strict=True) if stop - first == 1]


def lone_moves(starts: set[int], bar: int, total: int, fixed: set[int]) -> list[set[int]]:
    """Line starts to try for a 0-based lone bar: join a neighbour inside its section, take a bar from one, then cross a section mark."""
    later = min((s for s in starts if s > bar), default=None)
    earlier = max((s for s in starts if s < bar), default=None)
    join_before = starts - {bar} if earlier is not None else None
    join_after = starts - {later} if later is not None else None
    take_before = (starts - {bar}) | {bar - 1} if earlier is not None and bar - earlier >= 3 else None
    take_after = (starts - {later}) | {later + 1} if later is not None and later + 1 < total and (min((s for s in starts if s > later), default=total) - later) >= 3 else None
    ordered = [
        join_before if bar not in fixed else None,
        join_after if later not in fixed else None,
        take_before,
        take_after,
        join_before,
        join_after,
    ]
    return [move for move in ordered if move is not None]


def voices_sound(voices: list[list[Event]]) -> bool:
    return any(e.heads for v in voices for e in v)


def staff_of(bars: list[Bar]) -> int:
    """The staff cued piano or keys, otherwise the top; a top staff with no notes gives way to the one that has them."""
    for bar in bars:
        if any(PIANO_CUE.search("".join(run[0] for run in t.runs)) for t in bar.texts2):
            return 2
    if any(any(e.heads for v in b.voices2 for e in v) for b in bars) and not any(any(e.heads for v in b.voices for e in v) for b in bars):
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
            left = HINT_WORDS  # a cue can stack labels, and each keeps its own words
            continue
        if bold:  # a band cue sharing the text is no lyric
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
    """A tie or slur whose other end was removed would dangle."""
    events = [(e, h) for b in score.bars for v in b.voices for e in v for h in e.heads]
    open_ties = False
    for index, (_, head) in enumerate(events):
        following = events[index + 1][1] if index + 1 < len(events) else None
        if head.tie_stop and not open_ties:
            head.tie_stop = False
        if head.tie_start and not (following and following.tie_stop and following.step == head.step and following.octave == head.octave):
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
    """Bars the pianist plays with: any accent, and a fill or tutti cue through the bars until the next chord change."""
    found = {bar.number for bar in score.bars if any(event.accent for voices in (bar.voices, bar.voices2) for voice in voices for event in voice)}
    for at, bar in enumerate(score.bars):
        if any(HIT_CUE.search("".join(run[0] for run in text.runs)) for text in [*bar.texts, *bar.texts2]):
            found.add(bar.number)
            for later in score.bars[at + 1 :]:
                if later.chords or later.rehearsal or later.final:
                    break
                found.add(later.number)
    return found


def kept_parts(score: Score) -> list[Part]:
    """The intro, interlude and outro, plus every run of hit bars outside them."""
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
    """Turn a score into the piano sheet in place, and name what kept its notes."""
    found = kept_parts(score)
    keep = {n: part for part in found for n in range(part.first, part.last + 1)}
    for part in found:
        part.staff = staff_of([score.bars[n - 1] for n in range(part.first, part.last + 1)])
    for bar in score.bars:
        held = keep.get(bar.number)
        if held is None:
            empty_bar(bar)
        else:
            first, second = (bar.voices2, bar.voices) if held.staff == 2 else (bar.voices, bar.voices2)
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
