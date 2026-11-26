"""Render practice videos from the pipeline's published favourite sheets."""

from __future__ import annotations

import argparse
import bisect
import dataclasses
import math
import operator
import pathlib
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from typing import TYPE_CHECKING, Literal

import mido
import pymupdf

from muse import port, render, song, status, toolpath
from muse.chordseat import MPOS_UNITS

if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import IO

ZERO = 0
ONE = 1
TWO = 2
THREE = 3
QUARTER = 4
WIDTH = 3840
HEIGHT = 2160
FPS = 30
KEYS_HEIGHT = 420
SLOT = (HEIGHT - KEYS_HEIGHT) // TWO
LOW = 36
HIGH = 96
OCTAVE = 12
WHITE = frozenset({0, 2, 4, 5, 7, 9, 11})
COLOURS = {"chords": (0x8D, 0xB6, 0x00), "staff1": (0x56, 0xB4, 0xE9), "staff2": (0xCC, 0x79, 0xA7)}
PRIORITY = ("staff1", "staff2", "chords")
CURSOR = (0x00, 0x65, 0xBF)
BYTE = 255
CURSOR_ALPHA = 178 / BYTE
CURSOR_SPACES = 0.4
MIN_CURSOR_WIDTH = 2
MARGIN = 60
TAIL = 2.0
ORCHESTRA = (22, 28)
PAD = (22, 8)
WOODBLOCK = (21, 52)
CLICK_CHANNEL = 3
CLICK_PITCH = 76
CLICK = 70
ACCENT = 110
CLICK_TICKS = 24
SAMPLE_RATE = 48000
DEFAULT_TEMPO = 500000
DEFAULT_SIGNATURE = (4, 4)
PDF_DPI = 100
POINT_DPI = 72
INK_THRESHOLD = 128
LABEL_THRESHOLD = 200
MILLISECONDS = 1000
ROW_PRECISION = 1
LINE_TOLERANCE = 1.0
BLACK_HALF_WIDTH = 0.3
BLACK_HEIGHT = 0.62
KEY_EDGE = (0.2, 0.2, 0.2)
PAPER = (1.0, 1.0, 1.0)
BLACK = (0.0, 0.0, 0.0)
LABEL_SHADE = (0.35, 0.35, 0.35)
LABEL_RGB = bytes((90, 90, 90))
LABEL_LEFT = 14
LABEL_BOTTOM = 22
LABEL_SIZE = 44
SYNTH_TIMEOUT = 900
ENCODER_TIMEOUT = 900
MIDI_TYPE = 1
BANK_LSB = 32
MISSING_PRESET = re.compile(r"No preset found on channel\s+(\d+)\s*\[bank=(\d+)", re.IGNORECASE)
Role = Literal["staff1", "staff2", "chords"]
Colour = tuple[int, int, int]
MidiMessage = mido.Message | mido.MetaMessage
TickEvent = tuple[int, MidiMessage]
Playback = tuple[float, int]


class VideoRefusedError(ValueError):
    """A score, published sheet or sound cannot supply the required practice video."""


@dataclasses.dataclass(frozen=True)
class Box:
    x: float
    y: float
    width: float
    height: float
    page: int


@dataclasses.dataclass(frozen=True)
class Line:
    page: int
    top: float
    height: float
    left: float
    right: float


@dataclasses.dataclass(frozen=True)
class Crop:
    pixels: bytes
    width: int
    height: int
    upper: float
    zoom: float
    left: float


@dataclasses.dataclass(frozen=True)
class Note:
    start: float
    end: float
    pitch: int
    role: Role


def message_kind(message: MidiMessage) -> str:
    """Read a MIDI message's dynamically declared kind.

    Returns:
        The message kind from mido's public dictionary interface.

    """
    return str(message.dict()["type"])


def message_integer(message: MidiMessage, field: str) -> int:
    """Read an integer MIDI field through mido's dictionary interface.

    Returns:
        The requested integer field.

    """
    return int(message.dict()[field])


def channel_roles(xml: bytes) -> list[Role]:
    """Read channel roles in the score's declaration order.

    Returns:
        The chord or staff role for each declared channel.

    Raises:
        VideoRefusedError: a channel belongs to an unsupported staff.

    """
    roles: list[Role] = []
    for index, part in enumerate(ET.fromstring(xml).iter("Part"), start=ONE):
        for channel in part.iter("Channel"):
            if channel.get("name") == "harmony":
                roles.append("chords")
            elif index == ONE:
                roles.append("staff1")
            elif index == TWO:
                roles.append("staff2")
            else:
                message = f"unsupported staff {index}"
                raise VideoRefusedError(message)
    return roles


def score_roles(score: pathlib.Path) -> list[Role]:
    """Read the channel declarations from a compressed score.

    Returns:
        The roles in declaration order.

    """
    with zipfile.ZipFile(score) as archive:
        name = next(name for name in archive.namelist() if name.endswith(".mscx"))
        return channel_roles(archive.read(name))


def tick_tracks(midi: mido.MidiFile) -> list[list[TickEvent]]:
    """Expand each track's delta ticks without moving events between tracks.

    Returns:
        Tracks containing absolute ticks and messages.

    """
    tracks: list[list[TickEvent]] = []
    for track in midi.tracks:
        tick = ZERO
        events: list[TickEvent] = []
        for message in track:
            tick += message_integer(message, "time")
            events.append((tick, message))
        tracks.append(events)
    return tracks


def signature_map(tracks: list[list[TickEvent]]) -> list[tuple[int, int, int]]:
    """Collect every meter change across every track.

    Returns:
        The ordered tick, numerator and denominator changes, including the initial meter.

    """
    changes: dict[int, tuple[int, int]] = {}
    for tick, message in sorted((event for track in tracks for event in track), key=operator.itemgetter(ZERO)):
        if message_kind(message) == "time_signature":
            changes[tick] = (message_integer(message, "numerator"), message_integer(message, "denominator"))
    changes.setdefault(ZERO, next(iter(changes.values()), DEFAULT_SIGNATURE))
    return [(tick, *signature) for tick, signature in sorted(changes.items())]


def first_bar_ticks(tracks: list[list[TickEvent]], resolution: int) -> int:
    """Measure the count-in using the first declared time signature.

    Returns:
        The length of the count-in bar in ticks.

    """
    _, numerator, denominator = signature_map(tracks)[ZERO]
    return resolution * QUARTER * numerator // denominator


def count_in_seconds(tracks: list[list[TickEvent]], resolution: int) -> float:
    """Integrate the first bar through the score's complete tempo map.

    Returns:
        The count-in duration in seconds.

    """
    end = first_bar_ticks(tracks, resolution)
    tempo = DEFAULT_TEMPO
    previous = ZERO
    seconds = float(ZERO)
    changes = sorted(
        (
            (tick, message_integer(message, "tempo"))
            for track in tracks
            for tick, message in track
            if message_kind(message) == "set_tempo"
        ),
        key=operator.itemgetter(ZERO),
    )
    for tick, following in changes:
        if tick > end:
            break
        seconds += float(mido.tick2second(tick - previous, resolution, tempo))
        previous, tempo = tick, following
    return seconds + float(mido.tick2second(end - previous, resolution, tempo))


def click_positions(
    signatures: list[tuple[int, int, int]],
    resolution: int,
    end: int,
) -> list[tuple[int, int]]:
    """Place beats in each meter interval, accenting each bar's first beat.

    Returns:
        Click ticks and velocities before the exclusive end tick.

    """
    clicks: list[tuple[int, int]] = []
    for index, (start, numerator, denominator) in enumerate(signatures):
        stop = min(end, signatures[index + ONE][ZERO]) if index + ONE < len(signatures) else end
        beat = resolution * QUARTER // denominator
        for number, tick in enumerate(range(start, stop, beat)):
            clicks.append((tick, ACCENT if number % numerator == ZERO else CLICK))
    return clicks


def check_channels(tracks: list[list[TickEvent]], roles: list[Role]) -> None:
    """Refuse MIDI whose sounding channels differ from the declared channels.

    Raises:
        VideoRefusedError: the sounding channel set differs from declaration order.

    """
    used = {
        message_integer(message, "channel")
        for track in tracks
        for _, message in track
        if message_kind(message) == "note_on" and message_integer(message, "velocity") > ZERO
    }
    if used != set(range(len(roles))) or CLICK_CHANNEL in used:
        message = f"MIDI channels {sorted(used)} disagree with declarations {roles}"
        raise VideoRefusedError(message)


def preset_events(channel: int, preset: tuple[int, int]) -> list[TickEvent]:
    """Select the owner's bank before selecting its program.

    Returns:
        Ordered bank and program events at tick zero.

    """
    bank, program = preset
    return [
        (ZERO, mido.Message("control_change", channel=channel, control=ZERO, value=bank)),
        (ZERO, mido.Message("program_change", channel=channel, program=program)),
    ]


def audio_midi(midi: mido.MidiFile, roles: list[Role]) -> tuple[mido.MidiFile, float]:
    """Apply the owner's presets, count-in, tempo map and changing-meter clicks in ticks.

    Returns:
        The edited MIDI and its count-in duration.

    """
    tracks = tick_tracks(midi)
    check_channels(tracks, roles)
    bar = first_bar_ticks(tracks, midi.ticks_per_beat)
    end = max(tick for track in tracks for tick, _ in track)
    tempos = [(tick, message) for track in tracks for tick, message in track if message_kind(message) == "set_tempo"]
    shifted = [
        [
            (tick + bar, message)
            for tick, message in track
            if message_kind(message) not in {"program_change", "end_of_track"}
            and not (message_kind(message) == "control_change" and message_integer(message, "control") in {ZERO, BANK_LSB})
        ]
        for track in tracks
    ]
    setup = [
        event
        for channel, role in enumerate(roles)
        for event in preset_events(channel, PAD if role == "chords" else ORCHESTRA)
    ]
    shifted.insert(
        ZERO,
        [
            *setup,
            (bar, mido.MetaMessage("set_tempo", tempo=DEFAULT_TEMPO)),
            *((tick, message) for tick, message in tempos if tick < bar),
        ],
    )
    signatures = signature_map(tracks)
    _, numerator, denominator = signatures[ZERO]
    meters = [(ZERO, numerator, denominator), *((tick + bar, num, den) for tick, num, den in signatures)]
    clicks = preset_events(CLICK_CHANNEL, WOODBLOCK)
    for tick, velocity in click_positions(meters, midi.ticks_per_beat, end + bar):
        clicks.extend([
            (tick, mido.Message("note_on", channel=CLICK_CHANNEL, note=CLICK_PITCH, velocity=velocity)),
            (tick + CLICK_TICKS, mido.Message("note_off", channel=CLICK_CHANNEL, note=CLICK_PITCH)),
        ])
    shifted.append(clicks)
    result = mido.MidiFile(type=MIDI_TYPE, ticks_per_beat=midi.ticks_per_beat)
    for events in shifted:
        target = mido.MidiTrack()
        last = ZERO
        for tick, message in sorted(events, key=operator.itemgetter(ZERO)):
            target.append(message.copy(time=tick - last))
            last = tick
        result.tracks.append(target)
    return result, count_in_seconds(tracks, midi.ticks_per_beat)


def executable(name: str) -> str:
    """Resolve an installed rendering tool.

    Returns:
        The executable's absolute path.

    Raises:
        VideoRefusedError: the executable is unavailable.

    """
    found = toolpath.which(name)
    if found is None:
        message = f"{name}: executable unavailable"
        raise VideoRefusedError(message)
    return found


def render_audio(midi: mido.MidiFile, roles: list[Role], folder: pathlib.Path) -> float:
    """Render the edited MIDI through the owner's soundfonts.

    Returns:
        The count-in duration shared by picture and sound.

    Raises:
        VideoRefusedError: FluidSynth reports a missing nonzero-bank preset on a used channel.

    """
    edited, shift = audio_midi(midi, roles)
    destination = folder / "audio.mid"
    edited.save(destination)
    fonts = pathlib.Path.home() / "mslite" / "SoundFonts"
    done = subprocess.run(
        [
            executable("fluidsynth"),
            "-ni",
            "-q",
            "-r",
            str(SAMPLE_RATE),
            "-F",
            str(folder / "audio.wav"),
            str(fonts / "PianoLayer.sf2"),
            str(fonts / "Percussion.sf2"),
            str(destination),
        ],
        capture_output=True,
        check=True,
        timeout=SYNTH_TIMEOUT,
    )
    used = {*range(len(roles)), CLICK_CHANNEL}
    diagnostics = (done.stdout + done.stderr).decode(errors="replace")
    if any(int(channel) in used and int(bank) > ZERO for channel, bank in MISSING_PRESET.findall(diagnostics)):
        message = f"FluidSynth could not resolve an owner preset: {diagnostics}"
        raise VideoRefusedError(message)
    return shift


def elements(path: pathlib.Path) -> dict[int, Box]:
    """Read the pipeline's position boxes in PDF points.

    Returns:
        Element identifiers mapped to their page and geometry.

    """
    return {
        int(element.attrib["id"]): Box(
            float(element.attrib["x"]) / MPOS_UNITS,
            float(element.attrib["y"]) / MPOS_UNITS,
            float(element.attrib["sx"]) / MPOS_UNITS,
            float(element.attrib["sy"]) / MPOS_UNITS,
            int(element.attrib["page"]),
        )
        for element in ET.parse(path).getroot().iter("element")
    }


def playback(path: pathlib.Path, shift: float) -> list[Playback]:
    """Read segment visits in playback order with the count-in shift.

    Returns:
        Shifted seconds and element identifiers, preserving repeats and jumps.

    """
    return [
        (int(event.attrib["position"]) / MILLISECONDS + shift, int(event.attrib["elid"]))
        for event in ET.parse(path).getroot().iter("event")
    ]


def systems(measures: dict[int, Box]) -> list[Line]:
    """Group measure boxes into sheet lines in reading order.

    Returns:
        Each line's page and staff envelope.

    """
    rows: dict[tuple[int, float], list[Box]] = {}
    for box in measures.values():
        rows.setdefault((box.page, round(box.y, ROW_PRECISION)), []).append(box)
    return [
        Line(
            page,
            top,
            max(box.height for box in boxes),
            min(box.x for box in boxes),
            max(box.x + box.width for box in boxes),
        )
        for (page, top), boxes in sorted(rows.items())
    ]


def quietest_row(ink: Sequence[int], upper: float, lower: float) -> int:
    """Choose the middle of the least inked rows in a bounded interval.

    Returns:
        The split row, clamped to the image.

    """
    start = max(ZERO, min(len(ink) - ONE, int(upper)))
    stop = max(start, min(len(ink) - ONE, int(lower)))
    best = min(ink[start : stop + ONE])
    rows = [row for row in range(start, stop + ONE) if ink[row] == best]
    return rows[len(rows) // TWO]


def split_rows(document: pymupdf.Document, lines: list[Line], spatium: float) -> list[tuple[float, float]]:
    """Separate neighboring lines at the quietest ink rows.

    Returns:
        Upper and lower crop boundaries for every line.

    """
    profiles: dict[int, list[int]] = {}
    bands: list[tuple[float, float]] = []
    for index, line in enumerate(lines):
        page = document[line.page]
        if line.page not in profiles:
            pixmap = page.get_pixmap(dpi=POINT_DPI, colorspace=pymupdf.csGRAY)
            pixels = pixmap.samples
            profiles[line.page] = [
                sum(pixel < INK_THRESHOLD for pixel in pixels[row * pixmap.width : (row + ONE) * pixmap.width])
                for row in range(pixmap.height)
            ]
        ink = profiles[line.page]
        above = lines[index - ONE] if index > ZERO and lines[index - ONE].page == line.page else None
        below = lines[index + ONE] if index + ONE < len(lines) and lines[index + ONE].page == line.page else None
        upper = quietest_row(
            ink,
            above.top + above.height if above else page.rect.y0 + TWO,
            line.top if above else line.top - QUARTER * spatium,
        )
        lower = quietest_row(
            ink,
            line.top + line.height if below else line.top + line.height + QUARTER * spatium,
            below.top if below else page.rect.y1 - TWO,
        )
        bands.append((upper, lower))
    return bands


def crop_zoom(width: float, height: float) -> float:
    """Fit the whole crop into the available width and line slot.

    Returns:
        A uniform scale preserving both the entire height and width.

    """
    return min((WIDTH - TWO * MARGIN) / width, SLOT / height)


def crops(pdf: pathlib.Path, lines: list[Line], spatium: float) -> list[Crop]:
    """Invert complete line crops, scaling tall crops to fit their slot.

    Returns:
        RGB crops and their coordinate transforms.

    """
    left = min(line.left for line in lines) - TWO * spatium
    right = max(line.right for line in lines) + TWO * spatium
    result: list[Crop] = []
    with pymupdf.open(pdf) as document:
        for line, (upper, lower) in zip(lines, split_rows(document, lines, spatium), strict=True):
            zoom = crop_zoom(right - left, lower - upper)
            pixmap = document[line.page].get_pixmap(
                matrix=pymupdf.Matrix(zoom, zoom),
                clip=pymupdf.Rect(left, upper, right, lower),
                colorspace=pymupdf.csRGB,
            )
            if pixmap.height > SLOT:
                zoom *= (SLOT - ONE) / pixmap.height
                pixmap = document[line.page].get_pixmap(
                    matrix=pymupdf.Matrix(zoom, zoom),
                    clip=pymupdf.Rect(left, upper, right, lower),
                    colorspace=pymupdf.csRGB,
                )
            pixmap.invert_irect()
            result.append(Crop(pixmap.samples, pixmap.width, pixmap.height, upper, zoom, left))
    return result


def line_pair(current: int, count: int) -> tuple[int, ...]:
    """Put the visited line above its successor, including after a backward jump.

    Returns:
        The current and next line indices when available.

    """
    return tuple(line for line in (current, current + ONE) if line < count)


def cursor_x(
    seconds: float,
    index: int,
    timeline: list[Playback],
    segments: dict[int, Box],
    line_of: dict[int, int],
) -> float:
    """Interpolate on one line and hold before a change of line.

    Returns:
        The cursor's PDF x coordinate before its spatium offset.

    """
    start, element = timeline[index]
    left = segments[element].x
    if index + ONE < len(timeline):
        end, following = timeline[index + ONE]
        if line_of[following] == line_of[element] and end > start:
            return left + (segments[following].x - left) * (seconds - start) / (end - start)
    return left


def key_colour(roles: Sequence[Role]) -> Colour:
    """Resolve a sounding key by staff priority.

    Returns:
        The highest priority role's palette colour.

    """
    return COLOURS[min(roles, key=PRIORITY.index)]


def notes(midi: mido.MidiFile, roles: list[Role], shift: float) -> list[Note]:
    """Read sounding notes in seconds using every track's tempo events.

    Returns:
        Note intervals shifted by the same count-in as the picture.

    """
    opened: dict[tuple[int, int], list[float]] = {}
    result: list[Note] = []
    seconds = float(ZERO)
    for message in midi:
        seconds += float(message.dict()["time"])
        if message_kind(message) not in {"note_on", "note_off"}:
            continue
        channel = message_integer(message, "channel")
        pitch = message_integer(message, "note")
        key = (channel, pitch)
        starts = opened.get(key)
        if message_kind(message) == "note_on" and message_integer(message, "velocity") > ZERO:
            opened.setdefault(key, []).append(seconds)
        elif starts is not None and starts:
            start = starts.pop(ZERO)
            result.append(Note(start + shift, seconds + shift, pitch, roles[channel]))
    return sorted(result, key=lambda note: note.start)


KeyBox = tuple[int, int, int, int]


@dataclasses.dataclass(frozen=True)
class Keyboard:
    pixels: bytes
    boxes: dict[int, KeyBox]
    labels: list[int]


def keyboard_base() -> Keyboard:
    """Draw the keyboard and the C labels used over sounding keys.

    Returns:
        Unlit pixels, key rectangles and label pixel offsets.

    """
    whites = [pitch for pitch in range(LOW, HIGH + ONE) if pitch % OCTAVE in WHITE]
    width = WIDTH / len(whites)
    boxes: dict[int, KeyBox] = {}
    with pymupdf.open() as document, pymupdf.open() as label_document:
        page = document.new_page(width=WIDTH, height=KEYS_HEIGHT)
        labels = label_document.new_page(width=WIDTH, height=KEYS_HEIGHT)
        for index, pitch in enumerate(whites):
            boxes[pitch] = (int(index * width), ZERO, int((index + ONE) * width), KEYS_HEIGHT)
            page.draw_rect(pymupdf.Rect(*boxes[pitch]), color=KEY_EDGE, fill=PAPER, width=TWO)
        for pitch in range(LOW, HIGH + ONE):
            if pitch % OCTAVE not in WHITE:
                edge = boxes[pitch - ONE][TWO]
                boxes[pitch] = (
                    int(edge - width * BLACK_HALF_WIDTH),
                    ZERO,
                    int(edge + width * BLACK_HALF_WIDTH),
                    int(KEYS_HEIGHT * BLACK_HEIGHT),
                )
                page.draw_rect(pymupdf.Rect(*boxes[pitch]), color=BLACK, fill=BLACK, width=ZERO)
        for pitch in range(LOW, HIGH + ONE, OCTAVE):
            for target in (page, labels):
                target.insert_text(
                    (boxes[pitch][ZERO] + LABEL_LEFT, KEYS_HEIGHT - LABEL_BOTTOM),
                    f"C{pitch // OCTAVE - ONE}",
                    fontsize=LABEL_SIZE,
                    color=LABEL_SHADE,
                )
        pixels = page.get_pixmap(colorspace=pymupdf.csRGB).samples
        mask = labels.get_pixmap(colorspace=pymupdf.csGRAY).samples
        return Keyboard(pixels, boxes, [index * THREE for index, value in enumerate(mask) if value < LABEL_THRESHOLD])


def paint_key(pixels: bytearray, box: KeyBox, colour: Colour) -> None:
    """Fill one keyboard rectangle with a layer colour."""
    left, top, right, bottom = box
    row = bytes(colour) * (right - left)
    for y in range(top, bottom):
        start = (y * WIDTH + left) * THREE
        pixels[start : start + len(row)] = row


def keyboard_frame(keyboard: Keyboard, played: list[Note], seconds: float) -> bytearray:
    """Light every sounding key and preserve the black keys and C labels.

    Returns:
        The keyboard's RGB pixels for this instant.

    """
    pixels = bytearray(keyboard.pixels)
    sounding: dict[int, list[Role]] = {}
    for note in played:
        if note.start > seconds:
            break
        if seconds < note.end and LOW <= note.pitch <= HIGH:
            sounding.setdefault(note.pitch, []).append(note.role)
    for pitch in sorted(sounding, key=lambda pitch: pitch % OCTAVE not in WHITE):
        paint_key(pixels, keyboard.boxes[pitch], key_colour(sounding[pitch]))
    for pitch in range(LOW, HIGH + ONE):
        if pitch % OCTAVE not in WHITE and pitch not in sounding:
            paint_key(pixels, keyboard.boxes[pitch], (ZERO, ZERO, ZERO))
    for offset in keyboard.labels:
        pixels[offset : offset + THREE] = LABEL_RGB
    return pixels


@dataclasses.dataclass(frozen=True)
class Picture:
    lines: list[Line]
    images: list[Crop]
    segments: dict[int, Box]
    timeline: list[Playback]
    line_of: dict[int, int]
    spatium: float


def picture_inputs(folder: pathlib.Path, shift: float) -> Picture:
    """Load the rendered sheet and its own playback geometry.

    Returns:
        Complete picture inputs, including segment to line assignments.

    """
    lines = systems(elements(folder / "v.mpos"))
    spatium = min(line.height for line in lines) / QUARTER
    segments = elements(folder / "v.spos")
    line_of = {
        element: next(
            index
            for index, line in enumerate(lines)
            if line.page == box.page and line.top - LINE_TOLERANCE <= box.y <= line.top + line.height + LINE_TOLERANCE
        )
        for element, box in segments.items()
    }
    return Picture(
        lines,
        crops(folder / "v.pdf", lines, spatium),
        segments,
        playback(folder / "v.spos", shift),
        line_of,
        spatium,
    )


def paint_cursor(frame: bytearray, picture: Picture, index: int, seconds: float, top: int) -> None:
    """Blend the app's selection cursor over the current line."""
    line_index = picture.line_of[picture.timeline[index][ONE]]
    line, crop = picture.lines[line_index], picture.images[line_index]
    left = (WIDTH - crop.width) // TWO
    x = cursor_x(seconds, index, picture.timeline, picture.segments, picture.line_of)
    cx = left + int((x - picture.spatium - crop.left) * crop.zoom)
    upper = top + int((line.top - THREE * picture.spatium - crop.upper) * crop.zoom)
    lower = top + int((line.top + line.height + THREE * picture.spatium - crop.upper) * crop.zoom)
    width = max(MIN_CURSOR_WIDTH, int(CURSOR_SPACES * picture.spatium * crop.zoom))
    for y in range(max(top, upper), min(top + crop.height, lower)):
        for x_pixel in range(max(ZERO, cx), min(WIDTH, cx + width)):
            offset = (y * WIDTH + x_pixel) * THREE
            frame[offset : offset + THREE] = bytes(
                int(frame[offset + channel] * (ONE - CURSOR_ALPHA) + colour * CURSOR_ALPHA)
                for channel, colour in enumerate(CURSOR)
            )


def picture_frame(
    picture: Picture,
    keyboard: Keyboard,
    played: list[Note],
    seconds: float,
) -> bytearray:
    """Compose the keyboard, current line pair and time interpolated cursor.

    Returns:
        One complete RGB video frame.

    """
    frame = bytearray(WIDTH * HEIGHT * THREE)
    frame[: WIDTH * KEYS_HEIGHT * THREE] = keyboard_frame(keyboard, played, seconds)
    times = [event[ZERO] for event in picture.timeline]
    index = bisect.bisect_right(times, seconds) - ONE
    current = picture.line_of[picture.timeline[max(index, ZERO)][ONE]]
    for slot, line in enumerate(line_pair(current, len(picture.lines))):
        crop = picture.images[line]
        top = KEYS_HEIGHT + slot * SLOT + (SLOT - crop.height) // TWO
        left = (WIDTH - crop.width) // TWO
        stride = crop.width * THREE
        for row in range(crop.height):
            offset = ((top + row) * WIDTH + left) * THREE
            frame[offset : offset + stride] = crop.pixels[row * stride : (row + ONE) * stride]
        if slot == ZERO and index >= ZERO:
            paint_cursor(frame, picture, index, seconds, top)
    return frame


def write_frames(stream: IO[bytes] | None, picture: Picture, played: list[Note], end: float) -> None:
    """Write each picture frame and hold the final picture through the tail.

    Raises:
        VideoRefusedError: the encoder cannot accept frames.

    """
    if stream is None:
        message = "encoder has no frame input"
        raise VideoRefusedError(message)
    keyboard = keyboard_base()
    last_note = end - TAIL
    held: bytearray | None = None
    for number in range(math.ceil(end * FPS)):
        seconds = number / FPS
        if seconds >= last_note:
            if held is None:
                held = picture_frame(picture, keyboard, played, last_note)
            frame = held
        else:
            frame = picture_frame(picture, keyboard, played, seconds)
        stream.write(frame)
    stream.close()


def encode(folder: pathlib.Path, destination: pathlib.Path, played: list[Note], shift: float) -> None:
    """Stream the practice picture into hardware HEVC with the rendered AAC sound.

    Raises:
        CalledProcessError: the encoder exits with a nonzero status.
        VideoRefusedError: no notes exist.

    """
    if not played:
        message = "MIDI has no complete notes"
        raise VideoRefusedError(message)
    end = max(note.end for note in played) + TAIL
    picture = picture_inputs(folder, shift)
    command = [
        executable("ffmpeg"),
        "-loglevel",
        "error",
        "-y",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{WIDTH}x{HEIGHT}",
        "-r",
        str(FPS),
        "-i",
        "-",
        "-i",
        str(folder / "audio.wav"),
        "-c:v",
        "hevc_videotoolbox",
        "-b:v",
        "24M",
        "-tag:v",
        "hvc1",
        "-c:a",
        "aac",
        "-b:a",
        "256k",
        "-t",
        str(end),
        str(destination),
    ]
    with subprocess.Popen(command, stdin=subprocess.PIPE) as encoder:
        try:
            write_frames(encoder.stdin, picture, played, end)
            code = encoder.wait(timeout=ENCODER_TIMEOUT)
        except BaseException:
            encoder.kill()
            encoder.wait(timeout=ENCODER_TIMEOUT)
            raise
        if code != ZERO:
            raise subprocess.CalledProcessError(code, command)


def require_identical(rendered: pathlib.Path, published: pathlib.Path) -> None:
    """Refuse a sheet whose pages differ from the published favourite at the comparison resolution.

    Raises:
        VideoRefusedError: page counts, pixel dimensions or pixel values differ.

    """
    with pymupdf.open(rendered) as actual, pymupdf.open(published) as expected:
        same = len(actual) == len(expected)
        if same:
            for actual_page, expected_page in zip(actual, expected, strict=True):
                left = actual_page.get_pixmap(dpi=PDF_DPI)
                right = expected_page.get_pixmap(dpi=PDF_DPI)
                same = same and (left.width, left.height, left.samples) == (right.width, right.height, right.samples)
        if not same:
            message = f"{rendered}: differs from published favourite {published}"
            raise VideoRefusedError(message)


def render_song(source: pathlib.Path, sheets: pathlib.Path, work: pathlib.Path, videos: pathlib.Path) -> pathlib.Path:
    """Render one favourite sheet and refuse it before encoding if publication differs.

    Returns:
        The completed video's destination.

    """
    info = song.song_of(source)
    variant, size = status.favourite_size(info.name)
    final = work / f"{info.name}-{variant}-{size:g}-final.mscz"
    published = sheets / (info.tone_pdf if variant == "tone" else info.original_pdf)
    key = published.stem.rsplit("-", ONE)[-ONE]
    destination = videos / f"{info.name}-{key}.mp4"
    roles = score_roles(final)
    options = port.transpose_options(info) if variant == "tone" else None
    videos.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"{info.name}-video-", dir=videos) as temporary:
        folder = pathlib.Path(temporary)
        batcher = render.Batcher()
        with batcher.worker():
            batcher.render(final, [folder / f"v{suffix}" for suffix in (".pdf", ".mid", ".spos", ".mpos")], options)
        require_identical(folder / "v.pdf", published)
        midi = mido.MidiFile(folder / "v.mid")
        shift = render_audio(midi, roles, folder)
        encode(folder, folder / "video.mp4", notes(midi, roles, shift), shift)
        (folder / "video.mp4").replace(destination)
    return destination


def require_external(videos: pathlib.Path) -> None:
    """Refuse a video destination inside a repository.

    Raises:
        VideoRefusedError: the destination belongs to a repository.

    """
    folder = videos.resolve()
    if any((parent / ".git").exists() for parent in (folder, *folder.parents)):
        message = f"{videos}: videos must be outside repositories"
        raise VideoRefusedError(message)


class Arguments(argparse.Namespace):
    sources: pathlib.Path
    sheets: pathlib.Path
    work: pathlib.Path
    videos: pathlib.Path
    songs: list[str]


def main() -> None:
    """Render requested songs, or every discovered song when none are named."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sources", type=pathlib.Path)
    parser.add_argument("sheets", type=pathlib.Path)
    parser.add_argument("work", type=pathlib.Path)
    parser.add_argument("videos", type=pathlib.Path)
    parser.add_argument("songs", nargs="*")
    arguments = parser.parse_args(namespace=Arguments())
    require_external(arguments.videos)
    found = {source.stem.rsplit("-", ONE)[ZERO]: source for source in song.sources(arguments.sources)}
    wanted = arguments.songs or sorted(found)
    missing = set(wanted) - found.keys()
    if missing:
        parser.error(f"no source PDF for {sorted(missing)}")
    for name in wanted:
        destination = render_song(found[name], arguments.sheets, arguments.work, arguments.videos)
        sys.stdout.write(f"{destination}\n")


if __name__ == "__main__":
    main()
