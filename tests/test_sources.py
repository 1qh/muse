"""Decoders against the real sheets, kept outside the repo; MUSE_SOURCES must name them."""
import hashlib
import os
import pathlib
from fractions import Fraction as F

import pytest

from muse.port import decode

BARS = {"SongA-Cm": 95, "SongB-Em": 102, "SongC-Dm": 122, "SongD-Am": 63, "SongE-Dm": 100, "SongF-Fm": 98, "SongG-Em": 75, "SongH-Dm": 56}


def source(name):
    root = os.environ.get("MUSE_SOURCES")
    if not root:
        pytest.fail("MUSE_SOURCES is not set")
    return pathlib.Path(root) / f"{name}.pdf"


def reading(score):
    rows = []
    for b in score.bars:
        for voices in (b.voices, b.voices2):
            for v in voices:
                rows.append(" ".join(("+".join(f"{h.step}{h.alter}{h.octave}" for h in e.heads) or "r") + f":{e.length}" for e in v))
        rows.append("|".join(f"{c.root}{c.root_alter}{c.kind}/{c.bass}{c.bass_alter}" for c in b.chords))
        rows.append(f"{b.repeat_start}{b.repeat_end}{b.volta}{b.segno}{b.coda}{b.to_coda}{b.dal_segno}")
    return "\n".join(rows)


@pytest.mark.parametrize("name", sorted(BARS))
def test_bar_count_and_no_flags(name):
    score = decode(source(name))
    assert len(score.bars) == BARS[name]
    assert score.flags == []


@pytest.mark.parametrize("name", sorted(BARS))
def test_every_voice_fills_its_bar(name):
    for b in decode(source(name)).bars:
        for voices in (b.voices, b.voices2):
            for v in voices:
                assert sum((e.length for e in v), F(0)) == b.length, b.number


@pytest.mark.parametrize("name", sorted(BARS))
def test_reading_is_pinned(name):
    """Digest of the reading proven against the sources; contains no sheet content."""
    digest = hashlib.sha256(reading(decode(source(name))).encode()).hexdigest()[:16]
    assert digest == PINNED[name]


PINNED = {
    'SongA-Cm': '5328db82c42e3962',
    'SongB-Em': '96299b564e377418',
    'SongC-Dm': 'e84e64d2505c91fb',
    'SongD-Am': 'c37c5b2fb18c3d6a',
    'SongE-Dm': 'b3591fdeb83a74c5',
    'SongF-Fm': '79339cc923da82a4',
    'SongG-Em': '60060770bcc2a593',
    'SongH-Dm': '5aedeb548a1c4274',
}
