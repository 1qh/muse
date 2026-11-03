"""Decoders against the real sheets, kept outside the repo; MUSE_SOURCES must name them."""
import hashlib
import json
import os
import pathlib
from fractions import Fraction as F

import pytest

from muse.song import decode, song_of


def pins():
    path = os.environ.get("MUSE_PINS")
    if not path:
        pytest.fail("MUSE_PINS is not set")
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))


def names():
    path = os.environ.get("MUSE_PINS")
    return sorted(json.loads(pathlib.Path(path).read_text(encoding="utf-8"))["songs"]) if path else ["MUSE_PINS is not set"]


def source(name):
    root = os.environ.get("MUSE_SOURCES")
    if not root:
        pytest.fail("MUSE_SOURCES is not set")
    return pathlib.Path(root) / f"{name}.pdf"


def reading(score):
    """Stable text form of everything a decoder reads."""
    rows = []
    for b in score.bars:
        for voices in (b.voices, b.voices2):
            for v in voices:
                rows.append(" ".join(("+".join(f"{h.step}{h.alter}{h.octave}" for h in e.heads) or "r") + f":{e.length}" for e in v))
        rows.append("|".join(f"{c.root}{c.root_alter}{c.kind}/{c.bass}{c.bass_alter}" for c in b.chords))
        rows.append(f"{b.repeat_start}{b.repeat_end}{b.volta}{b.segno}{b.coda}{b.to_coda}{b.dal_segno}")
    return "\n".join(rows)


@pytest.mark.parametrize("name", names())
def test_bar_count_and_no_flags(name):
    score = decode(source(name))
    assert len(score.bars) == pins()["songs"][name]["bars"]
    assert score.flags == []


@pytest.mark.parametrize("name", names())
def test_every_voice_fills_its_bar(name):
    for b in decode(source(name)).bars:
        for voices in (b.voices, b.voices2):
            for v in voices:
                assert sum((e.length for e in v), F(0)) == b.length, b.number


def test_all_notes_of_a_beam_share_a_group():
    groups = [e.beam for b in decode(source(pins()["beam_song"])).bars[34:35] for v in b.voices2 for e in v if e.heads and e.length == F(1, 4)]
    assert len(set(groups)) == 4 and len(groups) == 16


@pytest.mark.parametrize("name", names())
def test_reading_is_pinned(name):
    """Digest of the reading proven against the sources; contains no sheet content."""
    digest = hashlib.sha256(reading(decode(source(name))).encode()).hexdigest()[:16]
    assert digest == pins()["songs"][name]["digest"]


@pytest.mark.parametrize("name", names())
def test_derivation_matches_the_confirmed_song_list(name):
    pin = pins()["songs"][name]
    song = song_of(source(name))
    assert (song.tone, song.fifths, song.direction, song.semis, song.title) == (pin["tone"], pin["fifths"], pin["direction"], pin["semis"], pin["title"])
