"""Write the score model as MusicXML."""

from __future__ import annotations

import itertools
import operator
import re
from fractions import Fraction
from typing import TYPE_CHECKING
from xml.sax.saxutils import escape

from muse import chordnames

if TYPE_CHECKING:
    from muse.model import Bar, Chord, Clef, Event, Head, Orchestra, Part, Score, Text

DIV = 48
EXACT_OVER_NOTE = 3.5
TYPES = {
    Fraction(4): "whole",
    Fraction(2): "half",
    Fraction(1): "quarter",
    Fraction(1, 2): "eighth",
    Fraction(1, 4): "16th",
    Fraction(1, 8): "32nd",
}
SHAPES = {"x": "x", "diamond": "diamond", "filleddiamond": "diamond", "circlex": "circle-x"}
COMPOUND_BEAT_TYPE = 8
SNAP_TO_NOTE_REACH = 9
FEWEST_POINTS_TO_SNAP = 2
TWO_STAVES = 2
LOWER_STAFF = 2


def _given_text(value: str | None) -> bool:
    return value is not None and bool(value)


def _given_number(value: int | None) -> bool:
    return value is not None and bool(value)


def kind_value(kind: str) -> str:
    return "major" if not kind else "other"


def harmony(chord: Chord, onset: Fraction) -> str:
    alter = f"<root-alter>{chord.root_alter}</root-alter>" if chord.root_alter else ""
    bass = ""
    if _given_text(chord.bass):
        b_alter = f"<bass-alter>{chord.bass_alter}</bass-alter>" if chord.bass_alter else ""
        bass = f"<bass><bass-step>{chord.bass}</bass-step>{b_alter}</bass>"
    offset = f"<offset>{int(onset * DIV)}</offset>" if onset else ""
    text = escape(chordnames.stored(chord.kind), {'"': "&quot;"})
    return (
        f"<harmony><root><root-step>{chord.root}</root-step>{alter}</root>"
        f'<kind text="{text}">{kind_value(chord.kind)}</kind>{bass}{offset}</harmony>'
    )


ARTICULATION_XML = {
    "tenuto": "<tenuto/>",
    "strong-accent-up": '<strong-accent type="up"/>',
    "strong-accent-down": '<strong-accent type="down"/>',
}
TECHNICAL_XML = {"up-bow": "<up-bow/>", "down-bow": "<down-bow/>", "harmonic": "<harmonic/>"}
WEDGES = {"crescendo": "crescendo", "diminuendo": "diminuendo", "wedge-stop": "stop"}
MAX_TEXT_SIZE = 11.0
TEMPO_TEXT = re.compile(r"(.*?)\s*♩\s*=\s*(\d+)")


def words(text: Text) -> str:
    """Render a text as MusicXML words, a printed tempo as words and a metronome mark; nothing for an empty text.

    The empty case is a text with nothing to print.

    Returns:
        The direction element, or an empty string for a text with nothing to print.

    """
    if not any(content.strip() for content, _, _ in text.runs):
        return ""
    place = "above" if text.above else "below"
    runs = []
    for content, bold, italic in text.runs:
        attrs = f'font-size="{min(text.size, MAX_TEXT_SIZE):g}"'
        if bold:
            attrs += ' font-weight="bold"'
        if italic:
            attrs += ' font-style="italic"'
        tempo = TEMPO_TEXT.fullmatch(content.strip())
        if tempo is None:
            runs.append(f"<words {attrs}>{escape(content)}</words>")
            continue
        if tempo[1]:
            runs.append(f"<words {attrs}>{escape(tempo[1])}</words>")
        metronome = f"<metronome><beat-unit>quarter</beat-unit><per-minute>{tempo[2]}</per-minute></metronome>"
        return (
            f'<direction placement="{place}"><direction-type>{"".join(runs)}</direction-type>'
            f'<direction-type>{metronome}</direction-type><sound tempo="{tempo[2]}"/></direction>'
        )
    return f'<direction placement="{place}"><direction-type>{"".join(runs)}</direction-type></direction>'


def time_modification(ev: Event) -> str:
    """State a tuplet's ratio as MusicXML states it: actual notes in the time of normal notes.

    Returns:
        The time-modification element, or an empty string for a note outside a tuplet.

    """
    return (
        f"<time-modification><actual-notes>{ev.ratio.denominator}</actual-notes>"
        f"<normal-notes>{ev.ratio.numerator}</normal-notes></time-modification>"
        if ev.tuplet
        else ""
    )


def _rest_xml(ev: Event, voice: int, dur: int, staff: str, base: Fraction) -> str:
    hidden = ' print-object="no"' if ev.hidden else ""
    rest = '<rest measure="yes"/>' if ev.bar_rest else "<rest/>"
    typ = "" if ev.bar_rest else f"<type>{TYPES[base]}</type>" + "<dot/>" * ev.dots
    fer = "<notations><fermata/></notations>" if ev.fermata else ""
    return (
        f"<note{hidden}>{rest}<duration>{dur}</duration><voice>{voice}</voice>"
        f"{typ}{time_modification(ev)}{staff}{fer}</note>"
    )


def _head_opening(ev: Event, h: Head, i: int, dur: int) -> list[str]:
    parts = ["<note>"]
    if i:
        parts.append("<chord/>")
    alter = f"<alter>{h.alter}</alter>" if h.alter else ""
    if ev.grace:
        parts.insert(1, "<grace/>")
        parts.append(f"<pitch><step>{h.step}</step>{alter}<octave>{h.octave}</octave></pitch>")
    else:
        parts.append(
            f"<pitch><step>{h.step}</step>{alter}<octave>{h.octave}</octave></pitch><duration>{dur}</duration>",
        )
    if h.tie_stop:
        parts.append('<tie type="stop"/>')
    if h.tie_start:
        parts.append('<tie type="start"/>')
    return parts


def _head_marks(ev: Event, h: Head, i: int, staff: str) -> list[str]:
    parts = []
    if _given_text(ev.stem):
        parts.append(f"<stem>{ev.stem}</stem>")
    if h.shape in SHAPES:
        parts.append(
            ('<notehead filled="no">' if h.shape == "diamond" else "<notehead>") + SHAPES[h.shape] + "</notehead>",
        )
    parts.append(staff)
    if _given_text(ev.beam_state) and not i:
        parts.append(f'<beam number="1">{ev.beam_state}</beam>')
    return parts


def _first_head_notations(ev: Event) -> list[str]:
    nota = []
    articulations = ["<accent/>"] * ev.accent + [ARTICULATION_XML[m] for m in ev.marks if m in ARTICULATION_XML]
    if articulations:
        nota.append("<articulations>" + "".join(articulations) + "</articulations>")
    technical = [TECHNICAL_XML[m] for m in ev.marks if m in TECHNICAL_XML]
    if technical:
        nota.append("<technical>" + "".join(technical) + "</technical>")
    if "tremolo" in ev.marks:
        nota.append('<ornaments><tremolo type="single">3</tremolo></ornaments>')
    if ev.trill:
        nota.append("<ornaments><trill-mark/></ornaments>")
    if ev.fermata:
        nota.append("<fermata/>")
    return nota


def _head_notations(ev: Event, h: Head, i: int) -> list[str]:
    nota = []
    if h.tie_stop:
        nota.append('<tied type="stop"/>')
    if h.tie_start:
        nota.append('<tied type="start"/>')
    if h.slur_stop:
        nota.append('<slur type="stop" number="1"/>')
    if h.slur_start:
        nota.append('<slur type="start" number="1"/>')
    if ev.tuplet and _given_text(ev.tuplet_state) and not i:
        nota.append(f'<tuplet type="{ev.tuplet_state}" bracket="no"/>')
    if "arpeggiate" in ev.marks:
        nota.append("<arpeggiate/>")
    if not i:
        nota.extend(_first_head_notations(ev))
    return nota


def note_xml(ev: Event, voice: int, div: int = DIV) -> str:
    """Render 1 event as MusicXML notes: a rest, or 1 note per head with the later heads as chord notes.

    Returns:
        The note elements of the event.

    Raises:
        ValueError: when the event's length is no whole number of divisions.

    """
    out = []
    base = ev.nominal
    dots = ev.dots
    base /= {0: Fraction(1), 1: Fraction(3, 2), 2: Fraction(7, 4)}[dots]
    exact = ev.length * div
    if exact.denominator != 1:
        msg = f"{ev.length} quarters is no whole number of {div} divisions"
        raise ValueError(msg)
    dur = int(exact)
    staff = f"<staff>{ev.staff}</staff>" if _given_number(ev.staff) else ""
    if not ev.heads:
        return _rest_xml(ev, voice, dur, staff, base)
    for i, h in enumerate(ev.heads):
        parts = _head_opening(ev, h, i, dur)
        parts.extend((f"<voice>{voice}</voice><type>{TYPES[base]}</type>" + "<dot/>" * dots, time_modification(ev)))
        parts.extend(_head_marks(ev, h, i, staff))
        nota = _head_notations(ev, h, i)
        if nota:
            parts.append("<notations>" + "".join(nota) + "</notations>")
        parts.append("</note>")
        out.append("".join(parts))
    return "".join(out)


def set_beam_states(voice: list[Event]) -> None:
    for i, ev in enumerate(voice):
        if ev.beam is None:
            continue
        prev = i > 0 and voice[i - 1].beam == ev.beam
        nxt = i + 1 < len(voice) and voice[i + 1].beam == ev.beam
        if prev or nxt:
            ev.beam_state = "continue" if prev and nxt else "end" if prev else "begin"
    run = Fraction(0)
    for ev in voice:
        if not ev.tuplet:
            run = Fraction(0)
            continue
        state = "start" if run == 0 else None
        run += ev.length
        if run in {Fraction(1, 2), Fraction(1), Fraction(2)}:
            state = "stop"
            run = Fraction(0)
        ev.tuplet_state = state


def beat_of(time: tuple[int, int] | None) -> Fraction:
    """Give the beat in quarters: dotted in compound time.

    Returns:
        The length of 1 beat in quarters.

    """
    if time and time[1] == COMPOUND_BEAT_TYPE and time[0] % 3 == 0:
        return Fraction(3, 2)
    return Fraction(4, time[1]) if time else Fraction(1)


def empty_bar_onsets(count: int, length: Fraction, time: tuple[int, int] | None) -> list[Fraction]:
    """Chords of a bar without notes, in printed order: a lone chord on beat 1, several evenly on real beats.

    Returns:
        The onset of each chord in quarters, in printed order.

    """
    step = beat_of(time)
    while count * step > length:
        step /= 2
    onsets: list[Fraction] = []
    for index in range(count):
        wanted = length * index / count
        at = round(wanted / step) * step
        onsets.append(max(at, onsets[-1] + step) if onsets else at)
    return onsets


def has_heads(voices: list[list[Event]]) -> bool:
    return any(e.heads for voice in voices for e in voice)


def lone_chord_onset(bar: Bar, voices: list[list[Event]], chord: Chord) -> Fraction:
    """Open the bar with a lone chord; only printing exactly over a note in the bar's 2nd half moves it there.

    Returns:
        The onset of the lone chord in quarters.

    """
    for event in sorted((e for voice in voices for e in voice if e.heads and not e.grace), key=lambda e: e.onset):
        if event.onset * 2 >= bar.length and abs(event.x - chord.x) <= EXACT_OVER_NOTE:
            return event.onset
    return Fraction(0)


def chord_onsets(bar: Bar, time: tuple[int, int] | None) -> list[tuple[Fraction, Chord]]:
    """Every chord of a bar with its true onset in quarters.

    Returns:
        Each chord paired with its onset in quarters, in onset order.

    """
    if not bar.chords:
        return []
    ordered = sorted(bar.chords, key=lambda c: c.x)
    if not has_heads(bar.voices) and not has_heads(bar.voices2):
        return list(zip(empty_bar_onsets(len(ordered), bar.length, time), ordered, strict=True))
    anchors = bar.voices if has_heads(bar.voices) else bar.voices2
    if len(ordered) == 1:
        return [(lone_chord_onset(bar, anchors, ordered[0]), ordered[0])]
    return sorted(((x_to_time(bar, anchors, c.x), c) for c in ordered), key=operator.itemgetter(0))


def x_to_time(bar: Bar, voices: list[list[Event]], x: float) -> Fraction:
    points: list[tuple[float, Fraction]] = [(bar.left, Fraction(0))]
    for voice in voices:
        points += [(e.x, e.onset) for e in voice if not e.bar_rest and not e.hidden]
    points.append((bar.right, bar.length))
    points.sort(key=operator.itemgetter(0))
    ok: list[tuple[float, Fraction]] = [points[0]]
    for p in points[1:]:
        if p[1] >= ok[-1][1]:
            ok.append(p)
    best = min(ok[1:-1] or ok[:1], key=lambda p: abs(p[0] - x))
    if abs(best[0] - x) < SNAP_TO_NOTE_REACH and len(ok) > FEWEST_POINTS_TO_SNAP:
        return best[1]
    for a, b in itertools.pairwise(ok):
        if a[0] <= x <= b[0] and b[0] > a[0]:
            frac = (x - a[0]) / (b[0] - a[0])
            t = a[1] + (b[1] - a[1]) * Fraction(frac).limit_denominator(64)
            return min(Fraction(round(t * 2), 2), bar.length - Fraction(1, 2))
    return Fraction(0)


def _left_barline(bar: Bar, part: int) -> list[str]:
    out = []
    if bar.repeat_start or _given_text(bar.volta):
        out.append('<barline location="left">')
        if _given_text(bar.volta) and part == 1:
            out.append(f'<ending number="{bar.volta}" type="start">{bar.volta}.</ending>')
        if bar.repeat_start:
            out.append('<bar-style>heavy-light</bar-style><repeat direction="forward"/>')
        out.append("</barline>")
    return out


def _measure_attributes(bar: Bar, score: Score) -> list[str]:
    attrs = []
    if bar.number == 1:
        attrs.append(f"<divisions>{DIV}</divisions>")
    if bar.key is not None:
        if score.minor is None:
            msg = "a key signature needs its mode, which the source's file name states"
            raise ValueError(msg)
        attrs.append(f"<key><fifths>{bar.key}</fifths><mode>{'minor' if score.minor else 'major'}</mode></key>")
    if bar.time:
        sym = f' symbol="{bar.time_symbol}"' if _given_text(bar.time_symbol) else ""
        attrs.append(f"<time{sym}><beats>{bar.time[0]}</beats><beat-type>{bar.time[1]}</beat-type></time>")
    if bar.number == 1:
        oct_change = f"<clef-octave-change>{score.clef_octave}</clef-octave-change>" if score.clef_octave else ""
        attrs.append(f"<clef><sign>G</sign><line>2</line>{oct_change}</clef>")
    if attrs:
        return ["<attributes>" + "".join(attrs) + "</attributes>"]
    return []


def _opening_directions(bar: Bar) -> list[str]:
    out = []
    if bar.tempo:
        label, bpm = bar.tempo[0], f"{bar.tempo[1]:g}"
        if label.strip():
            out.append(
                '<direction placement="above"><direction-type><words font-weight="bold" font-style="italic" '
                f'font-size="{MAX_TEXT_SIZE:g}">{escape(label)}</words></direction-type></direction>',
            )
        out.append(
            '<direction placement="above"><direction-type><metronome><beat-unit>quarter</beat-unit>'
            f'<per-minute>{bpm}</per-minute></metronome></direction-type><sound tempo="{bpm}"/></direction>',
        )
    out.extend(
        '<direction placement="above"><direction-type>'
        f'<rehearsal enclosure="square">{escape(mark)}</rehearsal></direction-type></direction>'
        for mark in bar.rehearsal
    )
    if bar.segno:
        out.append(
            '<direction placement="above"><direction-type><segno/></direction-type><sound segno="segno"/></direction>',
        )
    if bar.coda:
        out.append(
            '<direction placement="above"><direction-type><coda/></direction-type><sound coda="coda"/></direction>',
        )
    return out


def _texts_before(voice: list[Event], j: int, bar_texts: list[Text]) -> list[str]:
    out = []
    ev = voice[j]
    nxt = voice[j + 1].x if j + 1 < len(voice) else 1e9
    midpoint = (ev.x + nxt) / 2
    for text in [t for t in bar_texts if j + 1 == len(voice) or t.x < midpoint]:
        out.append(words(text))
        bar_texts.remove(text)
    return out


def _harmonies_at(voice: list[Event], j: int, chords: list[tuple[Fraction, Chord]]) -> list[str]:
    ev = voice[j]
    end = ev.onset + ev.length
    pending = [p for p in chords if ev.onset <= p[0] < end or (j == len(voice) - 1 and p[0] >= ev.onset)]
    out = [harmony(c, t - ev.onset) for t, c in pending]
    chords[:] = [p for p in chords if p not in pending]
    return out


def _wedge_starts(bar: Bar, voice: list[Event], j: int, wedge_events: list[tuple[float, float, str]]) -> list[str]:
    ev = voice[j]
    limit = voice[j + 1].x if j + 1 < len(voice) else bar.right + 1
    return [
        f'<direction placement="below"><direction-type><wedge type="{kind}"/></direction-type></direction>'
        for lo, _, kind in wedge_events
        if lo >= 0 and ((j == 0 and lo < ev.x) or ev.x <= lo < limit)
    ]


def _wedge_stops(bar: Bar, voice: list[Event], j: int, wedge_events: list[tuple[float, float, str]]) -> list[str]:
    ev = voice[j]
    limit = voice[j + 1].x if j + 1 < len(voice) else bar.right + 1
    return [
        '<direction placement="below"><direction-type><wedge type="stop"/></direction-type></direction>'
        for _, hi, _ in wedge_events
        if hi >= 0 and (ev.x <= hi < limit or (j == 0 and hi < ev.x))
    ]


def _voices_xml(bar: Bar, part: int, voices: list[list[Event]], time: tuple[int, int] | None) -> list[str]:
    out = []
    bar_texts = list(bar.texts if part == 1 else bar.texts2)
    for voice in voices:
        set_beam_states(voice)
    chords = chord_onsets(bar, bar.time or time) if part == 1 else []
    wedge_events = bar.wedges if part == 1 else []
    for i, voice in enumerate(voices):
        if i:
            out.append(
                f"<backup><duration>{int(sum((e.length for e in voices[i - 1]), Fraction(0)) * DIV)}</duration></backup>",
            )
        for j, ev in enumerate(voice):
            if i == 0:
                out.extend(_texts_before(voice, j, bar_texts))
                out.extend(_harmonies_at(voice, j, chords))
                out.extend(_wedge_starts(bar, voice, j, wedge_events))
            out.append(note_xml(ev, i + 1))
            if i == 0:
                out.extend(_wedge_stops(bar, voice, j, wedge_events))
    return out


def _closing_directions(bar: Bar) -> list[str]:
    out = []
    if bar.to_coda:
        out.append(
            '<direction placement="above"><direction-type><words>To Coda</words></direction-type>'
            '<sound tocoda="coda"/></direction>',
        )
    if bar.dal_segno:
        out.append(
            '<direction placement="above"><direction-type><words>D.S. al Coda</words></direction-type>'
            '<sound dalsegno="segno"/></direction>',
        )
    if bar.fine:
        out.append(
            '<direction placement="above"><direction-type><words>Fine</words></direction-type>'
            '<sound fine="yes"/></direction>',
        )
    return out


def _right_barline(bar: Bar, part: int) -> list[str]:
    out = []
    if bar.repeat_end or bar.final or bar.double or bar.volta_stop:
        style = "light-heavy" if bar.repeat_end or bar.final else "light-light" if bar.double else "regular"
        out.append(f'<barline location="right"><bar-style>{style}</bar-style>')
        if bar.volta_stop and part == 1:
            kind = "stop" if bar.repeat_end else "discontinue"
            out.append(f'<ending number="{bar.volta_stop}" type="{kind}"/>')
        if bar.repeat_end:
            out.append('<repeat direction="backward"/>')
        out.append("</barline>")
    return out


def measure(bar: Bar, part: int, score: Score, time: tuple[int, int] | None = None) -> str:
    voices = bar.voices if part == 1 else bar.voices2
    out = [f'<measure number="{bar.number}">']
    if bar.new_page or bar.new_system:
        out.append(f'<print {"new-page" if bar.new_page else "new-system"}="yes"/>')
    out.extend(_left_barline(bar, part))
    out.extend(_measure_attributes(bar, score))
    if part == 1:
        out.extend(_opening_directions(bar))
    out.extend(_voices_xml(bar, part, voices, time))
    if part == 1:
        out.extend(_closing_directions(bar))
    out.extend(_right_barline(bar, part))
    out.append("</measure>")
    return "".join(out)


def times(score: Score) -> list[tuple[int, int] | None]:
    """Give the time signature in force in each bar.

    Returns:
        The time signature in force in each bar, or None before the first one.

    """
    current = None
    found = []
    for bar in score.bars:
        current = bar.time or current
        found.append(current)
    return found


def write(score: Score) -> str:
    parts = '<score-part id="P1"><part-name print-object="no"></part-name></score-part>'
    if score.has_second_staff:
        parts += '<score-part id="P2"><part-name print-object="no"></part-name></score-part>'
    in_force = times(score)
    body = (
        '<part id="P1">' + "".join(measure(b, 1, score, t) for b, t in zip(score.bars, in_force, strict=True)) + "</part>"
    )
    if score.has_second_staff:
        body += (
            '<part id="P2">'
            + "".join(measure(b, 2, score, t) for b, t in zip(score.bars, in_force, strict=True))
            + "</part>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<score-partwise version="4.0"><part-list>{parts}</part-list>{body}</score-partwise>\n'
    )


ORCHESTRAL_DIV = 480
DYNAMICS = {
    "p",
    "pp",
    "ppp",
    "pppp",
    "ppppp",
    "pppppp",
    "f",
    "ff",
    "fff",
    "ffff",
    "fffff",
    "ffffff",
    "mp",
    "mf",
    "sf",
    "sfp",
    "sfz",
    "fz",
    "fp",
    "rf",
    "rfz",
    "sffz",
    "sfpp",
    "pf",
    "n",
    "sfzp",
}


def clef_xml(clef: Clef, number: int | None) -> str:
    sign, line, octave = clef
    change = f"<clef-octave-change>{octave}</clef-octave-change>" if octave else ""
    attribute = f' number="{number}"' if _given_number(number) else ""
    return f"<clef{attribute}><sign>{sign}</sign><line>{line}</line>{change}</clef>"


def direction_xml(text: Text, staff: int | None) -> str:
    """Render a dynamic as MusicXML's dynamics, any other text as words, on the staff it prints by.

    Returns:
        The direction element, or an empty string for a text with nothing to print.

    """
    on_staff = f"<staff>{staff}</staff>" if _given_number(staff) else ""
    if text.kind in WEDGES:
        return (
            '<direction placement="below"><direction-type>'
            f'<wedge type="{WEDGES[text.kind]}"/></direction-type>{on_staff}</direction>'
        )
    if text.kind == "dynamic":
        mark = text.runs[0][0]
        inner = f"<{mark}/>" if mark in DYNAMICS else f"<other-dynamics>{escape(mark)}</other-dynamics>"
        return (
            f'<direction placement="{"above" if text.above else "below"}"><direction-type>'
            f"<dynamics>{inner}</dynamics></direction-type>{on_staff}</direction>"
        )
    return words(text).replace("</direction>", f"{on_staff}</direction>")


def _orchestral_attributes(bar: Bar, part: Part, *, minor: bool | None, two: bool) -> list[str]:
    attrs = []
    if bar.number == 1:
        attrs.append(f"<divisions>{ORCHESTRAL_DIV}</divisions>")
    if bar.key is not None:
        if minor is None:
            msg = "a key signature needs its mode, which the source's file name states"
            raise ValueError(msg)
        attrs.append(f"<key><fifths>{bar.key}</fifths><mode>{'minor' if minor else 'major'}</mode></key>")
    if bar.time:
        attrs.append(f"<time><beats>{bar.time[0]}</beats><beat-type>{bar.time[1]}</beat-type></time>")
    if bar.number == 1 and two:
        attrs.append("<staves>2</staves>")
    attrs += [clef_xml(clef, number if two else None) for number, onset, clef in bar.clefs if onset == 0]
    if bar.number == 1 and part.transposition:
        shift = part.transposition
        octave = f"<octave-change>{shift.octave}</octave-change>" if shift.octave else ""
        attrs.append(
            f"<transpose><diatonic>{shift.diatonic}</diatonic>"
            f"<chromatic>{shift.chromatic}</chromatic>{octave}</transpose>",
        )
    if attrs:
        return ["<attributes>" + "".join(attrs) + "</attributes>"]
    return []


def _stream_events(
    voice: list[Event],
    changes: list[tuple[Fraction, int, Clef]],
    pending: list[tuple[Text, int]],
    number: int,
    *,
    two: bool,
) -> list[str]:
    out = []
    for index, event in enumerate(voice):
        while changes and changes[0][0] <= event.onset:
            _, number_of, clef = changes.pop(0)
            out.append("<attributes>" + clef_xml(clef, number_of if two else None) + "</attributes>")
        following = voice[index + 1].x if index + 1 < len(voice) else 1e9
        for item in [item for item in pending if index + 1 == len(voice) or item[0].x < (event.x + following) / 2]:
            out.append(direction_xml(item[0], item[1] if two else None))
            pending.remove(item)
        out.append(note_xml(event, number, ORCHESTRAL_DIV))
    return out


def _orchestral_streams(bar: Bar, *, two: bool) -> list[str]:
    out = []
    streams = [(1, voice) for voice in bar.voices] + [(2, voice) for voice in bar.voices2]
    texts = {1: sorted(bar.texts, key=lambda t: t.x), 2: sorted(bar.texts2, key=lambda t: t.x)}
    first_of = {staff: next((at for at, (s, _) in enumerate(streams) if s == staff), None) for staff in (1, 2)}
    for at, (staff, voice) in enumerate(streams):
        if at:
            out.append(f"<backup><duration>{int(bar.length * ORCHESTRAL_DIV)}</duration></backup>")
        set_beam_states(voice)
        orphaned = staff == 1 and at == 0 and first_of[2] is None
        changes = sorted(
            (onset, number, clef)
            for number, onset, clef in bar.clefs
            if onset > 0 and ((number == staff and first_of[staff] == at) or (number == LOWER_STAFF and orphaned))
        )
        pending = [(text, staff) for text in texts[staff]] if first_of[staff] == at else []
        if orphaned:
            pending = sorted([*pending, *((text, 2) for text in texts[2])], key=lambda item: item[0].x)
        number = 4 * (staff - 1) + 1 + sum(1 for s, _ in streams[:at] if s == staff)
        out.extend(_stream_events(voice, changes, pending, number, two=two))
        for _, number_of, clef in changes:
            out.append("<attributes>" + clef_xml(clef, number_of if two else None) + "</attributes>")
    return out


def orchestral_measure(bar: Bar, part: Part, *, minor: bool | None) -> str:
    """Render 1 bar of 1 part: every staff's voices, clefs where they change, and its texts and dynamics.

    The part's texts and dynamics go where they print.

    Returns:
        The measure element.

    Raises:
        ValueError: when the bar carries a key signature and the mode is unknown.

    """
    out = [f'<measure number="{bar.number}">']
    if bar.new_page or bar.new_system:
        out.append(f'<print {"new-page" if bar.new_page else "new-system"}="yes"/>')
    two = part.staves == TWO_STAVES
    out.extend(_orchestral_attributes(bar, part, minor=minor, two=two))
    out.extend(_orchestral_streams(bar, two=two))
    out.append("</measure>")
    return "".join(out)


def write_orchestra(orchestra: Orchestra) -> str:
    """Write a full score: 1 part per instrument in the source's staff order, each transposing part as printed.

    Each transposing part carries its transposition.

    Returns:
        The MusicXML document.

    """
    listed = "".join(
        f'<score-part id="P{at}"><part-name>{escape(part.name)}</part-name>'
        f"<part-abbreviation>{escape(part.abbreviation)}</part-abbreviation></score-part>"
        for at, part in enumerate(orchestra.parts, 1)
    )
    body = "".join(
        f'<part id="P{at}">'
        + "".join(orchestral_measure(bar, part, minor=orchestra.minor) for bar in part.bars)
        + "</part>"
        for at, part in enumerate(orchestra.parts, 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<score-partwise version="4.0"><part-list>{listed}</part-list>{body}</score-partwise>\n'
    )
