"""Pure controls for the practice video's timing, channel roles and picture rules."""

from __future__ import annotations

import itertools

import mido
import pytest

from muse import video

ZERO = 0
ONE = 1
TWO = 2
THREE = 3
FOUR = 4
FIVE = 5
SIX = 6
SEVEN = 7
RESOLUTION = 480
BAR = RESOLUTION * FOUR
HALF_BAR = RESOLUTION * TWO
SLOW_TEMPO = 1000000
FAST_TEMPO = 250000
COUNT_SECONDS = 2.5
NOTE = 60
VELOCITY = 80
LEFT = 10.0
RIGHT = 30.0
MIDPOINT = 20.0
OTHER_LINE_X = 5.0
POSITION_TIME = 1.0
NEXT_TIME = 3.0
MIDDLE_TIME = 2.0
AFTER_TIME = 4.0
LATER_TIME = 5.0
INK = (9, 8, 1, 7, 1, 6, 1, 9)
QUIET_ROW = 4
STAFF1_COLOUR = (0x56, 0xB4, 0xE9)
STAFF2_COLOUR = (0xCC, 0x79, 0xA7)
CHORD_COLOUR = (0x8D, 0xB6, 0x00)
WITH_HARMONY = b"""<museScore><Score>
<Part><Instrument><Channel/><Channel name="harmony"/></Instrument></Part>
<Part><Instrument><Channel/></Instrument></Part>
</Score></museScore>"""
WITHOUT_HARMONY = b"""<museScore><Score>
<Part><Instrument><Channel/></Instrument></Part>
<Part><Instrument><Channel/></Instrument></Part>
</Score></museScore>"""


def test_channel_declaration_roles_with_harmony() -> None:
    assert video.channel_roles(WITH_HARMONY) == ["staff1", "chords", "staff2"]


def test_channel_declaration_roles_without_harmony() -> None:
    assert video.channel_roles(WITHOUT_HARMONY) == ["staff1", "staff2"]


def test_clicks_follow_every_time_signature_and_accent_downbeats() -> None:
    midi = mido.MidiFile(ticks_per_beat=RESOLUTION)
    midi.tracks.extend([
        mido.MidiTrack([mido.MetaMessage("time_signature", numerator=FOUR, denominator=FOUR)]),
        mido.MidiTrack([mido.MetaMessage("time_signature", numerator=TWO, denominator=FOUR, time=BAR)]),
    ])
    signatures = video.signature_map(video.tick_tracks(midi))
    assert video.click_positions(signatures, RESOLUTION, BAR + HALF_BAR) == [
        (ZERO, video.ACCENT),
        (RESOLUTION, video.CLICK),
        (RESOLUTION * TWO, video.CLICK),
        (RESOLUTION * THREE, video.CLICK),
        (BAR, video.ACCENT),
        (BAR + RESOLUTION, video.CLICK),
    ]


def tempo_tracks() -> list[list[video.TickEvent]]:
    """Put tempo changes off the note track for the count-in control.

    Returns:
        Absolute events with a tempo change halfway through the first bar.

    """
    return [
        [(ZERO, mido.MetaMessage("time_signature", numerator=FOUR, denominator=FOUR))],
        [
            (ZERO, mido.MetaMessage("set_tempo", tempo=SLOW_TEMPO)),
            (HALF_BAR, mido.MetaMessage("set_tempo", tempo=FAST_TEMPO)),
        ],
    ]


def test_count_in_integrates_the_tempo_map() -> None:
    assert video.first_bar_ticks(tempo_tracks(), RESOLUTION) == BAR
    assert video.count_in_seconds(tempo_tracks(), RESOLUTION) == pytest.approx(COUNT_SECONDS)


def test_audio_uses_the_same_count_in_and_keeps_the_source_tempo_map() -> None:
    midi = mido.MidiFile(ticks_per_beat=RESOLUTION)
    midi.tracks.extend([
        mido.MidiTrack([
            mido.Message("note_on", note=NOTE, velocity=VELOCITY),
            mido.Message("note_off", note=NOTE, time=BAR),
        ]),
        mido.MidiTrack([
            mido.MetaMessage("time_signature", numerator=FOUR, denominator=FOUR),
            mido.MetaMessage("set_tempo", tempo=SLOW_TEMPO),
            mido.MetaMessage("set_tempo", tempo=FAST_TEMPO, time=HALF_BAR),
        ]),
    ])
    edited, shift = video.audio_midi(midi, ["staff1"])
    seconds = float(ZERO)
    note_times: list[float] = []
    for message in edited:
        seconds += float(message.dict()["time"])
        if video.message_kind(message) in {"note_on", "note_off"} and video.message_integer(message, "channel") == ZERO:
            note_times.append(seconds)
    assert shift == pytest.approx(COUNT_SECONDS)
    assert note_times == pytest.approx([COUNT_SECONDS, COUNT_SECONDS * TWO])


def test_audio_meter_change_also_follows_the_count_in() -> None:
    midi = mido.MidiFile(ticks_per_beat=RESOLUTION)
    midi.tracks.extend([
        mido.MidiTrack([
            mido.Message("note_on", note=NOTE, velocity=VELOCITY),
            mido.Message("note_off", note=NOTE, time=BAR + HALF_BAR),
        ]),
        mido.MidiTrack([
            mido.MetaMessage("time_signature", numerator=FOUR, denominator=FOUR),
            mido.MetaMessage("time_signature", numerator=TWO, denominator=FOUR, time=BAR),
        ]),
    ])
    edited, _ = video.audio_midi(midi, ["staff1"])
    clicks = [
        (tick, video.message_integer(message, "velocity"))
        for track in video.tick_tracks(edited)
        for tick, message in track
        if video.message_kind(message) == "note_on" and video.message_integer(message, "channel") == video.CLICK_CHANNEL
    ]
    assert clicks == [
        (ZERO, video.ACCENT),
        (RESOLUTION, video.CLICK),
        (RESOLUTION * TWO, video.CLICK),
        (RESOLUTION * THREE, video.CLICK),
        (BAR, video.ACCENT),
        (BAR + RESOLUTION, video.CLICK),
        (BAR + RESOLUTION * TWO, video.CLICK),
        (BAR + RESOLUTION * THREE, video.CLICK),
        (BAR * TWO, video.ACCENT),
        (BAR * TWO + RESOLUTION, video.CLICK),
    ]


def test_channel_count_mismatch_is_refused() -> None:
    events: list[list[video.TickEvent]] = [[(ZERO, mido.Message("note_on", note=NOTE, velocity=VELOCITY))]]
    with pytest.raises(video.VideoRefusedError, match="disagree"):
        video.check_channels(events, ["staff1", "staff2"])


def test_line_pair_tracks_the_current_line_and_backward_jumps() -> None:
    visits = [ZERO, ONE, THREE, ONE, ZERO, FOUR]
    assert [video.line_pair(current, FIVE) for current in visits] == [
        (ZERO, ONE),
        (ONE, TWO),
        (THREE, FOUR),
        (ONE, TWO),
        (ZERO, ONE),
        (FOUR,),
    ]


def test_cursor_interpolates_on_one_line_and_holds_before_another() -> None:
    segments = {
        ZERO: video.Box(LEFT, ZERO, ONE, ONE, ZERO),
        ONE: video.Box(RIGHT, ZERO, ONE, ONE, ZERO),
        TWO: video.Box(OTHER_LINE_X, ONE, ONE, ONE, ZERO),
    }
    timeline = [(POSITION_TIME, ZERO), (NEXT_TIME, ONE), (LATER_TIME, TWO)]
    line_of = {ZERO: ZERO, ONE: ZERO, TWO: ONE}
    assert video.cursor_x(MIDDLE_TIME, ZERO, timeline, segments, line_of) == MIDPOINT
    assert video.cursor_x(AFTER_TIME, ONE, timeline, segments, line_of) == RIGHT
    assert video.cursor_x(LATER_TIME, TWO, timeline, segments, line_of) == OTHER_LINE_X


def test_split_chooses_middle_quietest_ink_row_within_bounds() -> None:
    assert video.quietest_row(INK, ONE, SIX) == QUIET_ROW
    assert video.quietest_row(INK, FIVE, SEVEN) == SIX
    assert video.quietest_row(INK, -ONE, ONE) == ONE


def test_key_colours_follow_staff_priority_in_any_note_order() -> None:
    roles: tuple[video.Role, ...] = ("chords", "staff2", "staff1")
    for order in itertools.permutations(roles):
        assert video.key_colour(order) == STAFF1_COLOUR
    assert video.key_colour(["chords", "staff2"]) == STAFF2_COLOUR
    assert video.key_colour(["chords"]) == CHORD_COLOUR


def test_tall_crop_scales_to_fit_the_whole_height() -> None:
    width = video.WIDTH - TWO * video.MARGIN
    height = video.SLOT * TWO
    zoom = video.crop_zoom(width, height)
    assert zoom * height == pytest.approx(video.SLOT)
    assert zoom * width <= width


def test_preset_selects_bank_before_program() -> None:
    events = video.preset_events(ZERO, video.ORCHESTRA)
    bank_tick, bank_message = events[ZERO]
    program_tick, program_message = events[ONE]
    assert (bank_tick, program_tick) == (ZERO, ZERO)
    assert video.message_kind(bank_message) == "control_change"
    assert video.message_integer(bank_message, "control") == ZERO
    assert video.message_integer(bank_message, "value") == video.ORCHESTRA[ZERO]
    assert video.message_kind(program_message) == "program_change"
    assert video.message_integer(program_message, "program") == video.ORCHESTRA[ONE]
