"""Write the score model as MusicXML."""
from __future__ import annotations

from fractions import Fraction as F
from xml.sax.saxutils import escape

from muse.model import Bar, Chord, Event, Score, Text

DIV = 48
EXACT_OVER_NOTE = 3.5  # points; chords on beat 1 commonly print further than this from their note
TYPES = {F(4): "whole", F(2): "half", F(1): "quarter", F(1, 2): "eighth", F(1, 4): "16th", F(1, 8): "32nd"}
SHAPES = {"x": "x", "diamond": "diamond", "filleddiamond": "diamond", "circlex": "circle-x"}


def kind_value(kind: str) -> str:
    return "major" if not kind else "other"


def harmony(chord: Chord, onset: F) -> str:
    alter = f"<root-alter>{chord.root_alter}</root-alter>" if chord.root_alter else ""
    bass = ""
    if chord.bass:
        b_alter = f"<bass-alter>{chord.bass_alter}</bass-alter>" if chord.bass_alter else ""
        bass = f"<bass><bass-step>{chord.bass}</bass-step>{b_alter}</bass>"
    offset = f"<offset>{int(onset * DIV)}</offset>" if onset else ""
    text = escape(chord.kind, {'"': "&quot;"})
    return f'<harmony><root><root-step>{chord.root}</root-step>{alter}</root><kind text="{text}">{kind_value(chord.kind)}</kind>{bass}{offset}</harmony>'


MAX_TEXT_SIZE = 11.0  # every text stays below the chords


def words(text: Text, staff_offset: int = 0) -> str:
    place = "above" if text.above else "below"
    runs = []
    for content, bold, italic in text.runs:
        attrs = f'font-size="{min(text.size, MAX_TEXT_SIZE):g}"'
        if bold:
            attrs += ' font-weight="bold"'
        if italic:
            attrs += ' font-style="italic"'
        runs.append(f"<words {attrs}>{escape(content)}</words>")
    return f'<direction placement="{place}"><direction-type>{"".join(runs)}</direction-type></direction>'


def note_xml(ev: Event, voice: int) -> str:
    out = []
    base = ev.nominal
    dots = ev.dots
    base = base / {0: F(1), 1: F(3, 2), 2: F(7, 4)}[dots]
    dur = int(ev.length * DIV)
    if not ev.heads:
        hidden = ' print-object="no"' if ev.hidden else ""
        rest = '<rest measure="yes"/>' if ev.bar_rest else "<rest/>"
        typ = "" if ev.bar_rest else f"<type>{TYPES[base]}</type>" + "<dot/>" * dots
        mod = "<time-modification><actual-notes>3</actual-notes><normal-notes>2</normal-notes></time-modification>" if ev.tuplet else ""
        fer = "<notations><fermata/></notations>" if ev.fermata else ""
        return f"<note{hidden}>{rest}<duration>{dur}</duration><voice>{voice}</voice>{typ}{mod}{fer}</note>"
    for i, h in enumerate(ev.heads):
        parts = ["<note>"]
        if i:
            parts.append("<chord/>")
        alter = f"<alter>{h.alter}</alter>" if h.alter else ""
        if ev.grace:
            parts.insert(1 if not i else 1, "<grace/>")
            parts.append(f"<pitch><step>{h.step}</step>{alter}<octave>{h.octave}</octave></pitch>")
        else:
            parts.append(f"<pitch><step>{h.step}</step>{alter}<octave>{h.octave}</octave></pitch><duration>{dur}</duration>")
        if h.tie_stop:
            parts.append('<tie type="stop"/>')
        if h.tie_start:
            parts.append('<tie type="start"/>')
        parts.append(f"<voice>{voice}</voice><type>{TYPES[base]}</type>" + "<dot/>" * dots)
        if ev.tuplet:
            parts.append("<time-modification><actual-notes>3</actual-notes><normal-notes>2</normal-notes></time-modification>")
        if ev.stem:
            parts.append(f"<stem>{ev.stem}</stem>")
        if h.shape in SHAPES:
            parts.append(('<notehead filled="no">' if h.shape == "diamond" else "<notehead>") + SHAPES[h.shape] + "</notehead>")
        if ev.beam_state and not i:
            parts.append(f'<beam number="1">{ev.beam_state}</beam>')
        nota = []
        if h.tie_stop:
            nota.append('<tied type="stop"/>')
        if h.tie_start:
            nota.append('<tied type="start"/>')
        if h.slur_stop:
            nota.append('<slur type="stop" number="1"/>')
        if h.slur_start:
            nota.append('<slur type="start" number="1"/>')
        if ev.tuplet and ev.tuplet_state and not i:
            nota.append(f'<tuplet type="{ev.tuplet_state}" bracket="no"/>')
        if not i:
            if ev.accent:
                nota.append("<articulations><accent/></articulations>")
            if ev.trill:
                nota.append("<ornaments><trill-mark/></ornaments>")
            if ev.fermata:
                nota.append("<fermata/>")
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
    run = F(0)
    for ev in voice:
        if not ev.tuplet:
            run = F(0)
            continue
        state = "start" if run == 0 else None
        run += ev.length
        if run in (F(1, 2), F(1), F(2)):
            state = "stop"
            run = F(0)
        ev.tuplet_state = state


def beat_of(time: tuple[int, int] | None) -> F:
    """The beat in quarters: dotted in compound time."""
    if time and time[1] == 8 and time[0] % 3 == 0:
        return F(3, 2)
    return F(4, time[1]) if time else F(1)


def empty_bar_onsets(count: int, length: F, time: tuple[int, int] | None) -> list[F]:
    """Chords of a bar without notes, in printed order: a lone chord on beat 1, several evenly on real beats."""
    step = beat_of(time)
    while count * step > length:
        step /= 2
    onsets: list[F] = []
    for index in range(count):
        wanted = length * index / count
        at = round(wanted / step) * step
        onsets.append(max(at, onsets[-1] + step) if onsets else at)
    return onsets


def has_heads(voices: list[list[Event]]) -> bool:
    return any(e.heads for voice in voices for e in voice)


def lone_chord_onset(bar: Bar, voices: list[list[Event]], chord: Chord) -> F:
    """A lone chord opens the bar; only printing exactly over a note in the bar's 2nd half moves it there."""
    for event in sorted((e for voice in voices for e in voice if e.heads and not e.grace), key=lambda e: e.onset):
        if event.onset * 2 >= bar.length and abs(event.x - chord.x) <= EXACT_OVER_NOTE:
            return event.onset
    return F(0)


def chord_onsets(bar: Bar, time: tuple[int, int] | None) -> list[tuple[F, Chord]]:
    """Every chord of a bar with its true onset in quarters."""
    if not bar.chords:
        return []
    ordered = sorted(bar.chords, key=lambda c: c.x)
    if not has_heads(bar.voices) and not has_heads(bar.voices2):
        return list(zip(empty_bar_onsets(len(ordered), bar.length, time), ordered, strict=True))
    anchors = bar.voices if has_heads(bar.voices) else bar.voices2
    if len(ordered) == 1:
        return [(lone_chord_onset(bar, anchors, ordered[0]), ordered[0])]
    return sorted(((x_to_time(bar, anchors, c.x), c) for c in ordered), key=lambda p: p[0])


def x_to_time(bar: Bar, voices: list[list[Event]], x: float) -> F:
    points = [(bar.left, F(0))]
    for voice in voices:
        points += [(e.x, e.onset) for e in voice if not e.bar_rest and not e.hidden]
    points.append((bar.right, bar.length))
    points.sort(key=lambda p: p[0])
    ok = [points[0]]
    for p in points[1:]:
        if p[1] >= ok[-1][1]:
            ok.append(p)
    # a chord printed over a note starts with it
    best = min(ok[1:-1] or ok[:1], key=lambda p: abs(p[0] - x))
    if abs(best[0] - x) < 9 and len(ok) > 2:
        return best[1]
    for a, b in zip(ok, ok[1:], strict=False):
        if a[0] <= x <= b[0] and b[0] > a[0]:
            frac = (x - a[0]) / (b[0] - a[0])
            t = a[1] + (b[1] - a[1]) * F(frac).limit_denominator(64)
            return min(F(round(t * 2), 2), bar.length - F(1, 2))
    return F(0)


def measure(bar: Bar, part: int, score: Score, time: tuple[int, int] | None = None) -> str:
    voices = bar.voices if part == 1 else bar.voices2
    out = [f'<measure number="{bar.number}">']
    if bar.new_page or bar.new_system:
        out.append(f'<print {"new-page" if bar.new_page else "new-system"}="yes"/>')
    if bar.repeat_start or bar.volta:
        out.append('<barline location="left">')
        if bar.volta and part == 1:  # MuseScore 3 prints a second staff's copy even when that staff hides
            out.append(f'<ending number="{bar.volta}" type="start">{bar.volta}.</ending>')
        if bar.repeat_start:
            out.append('<bar-style>heavy-light</bar-style><repeat direction="forward"/>')
        out.append("</barline>")
    attrs = []
    if bar.number == 1:
        attrs.append(f"<divisions>{DIV}</divisions>")
    if bar.key is not None:
        attrs.append(f"<key><fifths>{bar.key}</fifths><mode>minor</mode></key>")
    if bar.time:
        sym = f' symbol="{bar.time_symbol}"' if bar.time_symbol else ""
        attrs.append(f'<time{sym}><beats>{bar.time[0]}</beats><beat-type>{bar.time[1]}</beat-type></time>')
    if bar.number == 1:
        oct_change = f"<clef-octave-change>{score.clef_octave}</clef-octave-change>" if score.clef_octave else ""
        attrs.append(f"<clef><sign>G</sign><line>2</line>{oct_change}</clef>")
    if attrs:
        out.append("<attributes>" + "".join(attrs) + "</attributes>")
    if part == 1:
        if bar.tempo:
            label, bpm = bar.tempo[0], f"{bar.tempo[1]:g}"
            out.append(f'<direction placement="above"><direction-type><words font-weight="bold" font-style="italic" font-size="{MAX_TEXT_SIZE:g}">{escape(label)}</words></direction-type></direction>')
            out.append(f'<direction placement="above"><direction-type><metronome><beat-unit>quarter</beat-unit><per-minute>{bpm}</per-minute></metronome></direction-type><sound tempo="{bpm}"/></direction>')
        for mark in bar.rehearsal:
            out.append(f'<direction placement="above"><direction-type><rehearsal enclosure="square">{escape(mark)}</rehearsal></direction-type></direction>')
        if bar.segno:
            out.append('<direction placement="above"><direction-type><segno/></direction-type><sound segno="segno"/></direction>')
        if bar.coda:
            out.append('<direction placement="above"><direction-type><coda/></direction-type><sound coda="coda"/></direction>')
    bar_texts = list(bar.texts if part == 1 else bar.texts2)
    for voice in voices:
        set_beam_states(voice)
    chords = chord_onsets(bar, bar.time or time) if part == 1 else []
    wedge_events = bar.wedges if part == 1 else []
    for i, voice in enumerate(voices):
        if i:
            out.append(f"<backup><duration>{int(sum((e.length for e in voices[i - 1]), F(0)) * DIV)}</duration></backup>")
        for j, ev in enumerate(voice):
            if i == 0:
                nxt = voice[j + 1].x if j + 1 < len(voice) else 1e9
                midpoint = (ev.x + nxt) / 2
                for text in [t for t in bar_texts if j + 1 == len(voice) or t.x < midpoint]:
                    out.append(words(text))
                    bar_texts.remove(text)
                end = ev.onset + ev.length
                pending = [p for p in chords if ev.onset <= p[0] < end or (j == len(voice) - 1 and p[0] >= ev.onset)]
                for t, c in pending:
                    out.append(harmony(c, t - ev.onset))
                chords = [p for p in chords if p not in pending]
                limit = voice[j + 1].x if j + 1 < len(voice) else bar.right + 1
                for lo, _, kind in wedge_events:
                    if lo >= 0 and (j == 0 and lo < ev.x or ev.x <= lo < limit):
                        out.append(f'<direction placement="below"><direction-type><wedge type="{kind}"/></direction-type></direction>')
            out.append(note_xml(ev, i + 1))
            if i == 0:
                limit = voice[j + 1].x if j + 1 < len(voice) else bar.right + 1
                for _, hi, _ in wedge_events:
                    if hi >= 0 and (ev.x <= hi < limit or (j == 0 and hi < ev.x)):
                        out.append('<direction placement="below"><direction-type><wedge type="stop"/></direction-type></direction>')
    if part == 1:
        if bar.to_coda:
            out.append('<direction placement="above"><direction-type><words>To Coda</words></direction-type><sound tocoda="coda"/></direction>')
        if bar.dal_segno:
            out.append('<direction placement="above"><direction-type><words>D.S. al Coda</words></direction-type><sound dalsegno="segno"/></direction>')
        if bar.fine:
            out.append('<direction placement="above"><direction-type><words>Fine</words></direction-type><sound fine="yes"/></direction>')
    if bar.repeat_end or bar.final or bar.double or bar.volta_stop:
        style = "light-heavy" if bar.repeat_end or bar.final else "light-light" if bar.double else "regular"
        out.append(f'<barline location="right"><bar-style>{style}</bar-style>')
        if bar.volta_stop and part == 1:
            kind = "stop" if bar.repeat_end else "discontinue"
            out.append(f'<ending number="{bar.volta_stop}" type="{kind}"/>')
        if bar.repeat_end:
            out.append('<repeat direction="backward"/>')
        out.append("</barline>")
    out.append("</measure>")
    return "".join(out)


def times(score: Score) -> list[tuple[int, int] | None]:
    """The time signature in force in each bar."""
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
    body = '<part id="P1">' + "".join(measure(b, 1, score, t) for b, t in zip(score.bars, in_force, strict=True)) + "</part>"
    if score.has_second_staff:
        body += '<part id="P2">' + "".join(measure(b, 2, score, t) for b, t in zip(score.bars, in_force, strict=True)) + "</part>"
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<score-partwise version="4.0"><part-list>{parts}</part-list>{body}</score-partwise>\n'
