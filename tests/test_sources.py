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
    for folder in root.split(os.pathsep):
        path = pathlib.Path(folder) / f"{name}.pdf"
        if path.exists():
            return path
    pytest.fail(f"{name} is in no source folder")


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


def test_no_private_credit_reaches_the_decoded_text() -> None:
    names = (pathlib.Path(os.environ["MUSE_PINS"]).parent / "banned.txt").read_text(encoding="utf-8").split("\n")
    for name in sorted(pins()["songs"]):
        score = decode(source(name))
        words = " ".join(run[0] for bar in score.bars for text in [*bar.texts, *bar.texts2] for run in text.runs)
        assert not [word for word in names if word.strip() and word in words], name


def test_no_cue_joins_a_word_across_two_source_spans() -> None:
    root = os.environ["MUSE_SOURCES"]
    joined = []
    for folder in root.split(":"):
        for path in sorted(pathlib.Path(folder).glob("*.pdf")):
            for bar in decode(path).bars:
                for text in [*bar.texts, *bar.texts2]:
                    joined += [(path.stem, bar.number) for left, right in zip(text.runs, text.runs[1:], strict=False) if left[0] and right[0] and left[0][-1].isalnum() and right[0][0].isalnum()]
    assert not joined


def test_every_source_reads_with_no_flag() -> None:
    flagged = {path.stem: [issue.message for issue in decode(path).flags] for folder in os.environ["MUSE_SOURCES"].split(os.pathsep) for path in pathlib.Path(folder).glob("*.pdf")}
    assert not {name: flags for name, flags in flagged.items() if flags}


def test_every_change_of_bar_length_prints_its_time_signature() -> None:
    silent = []
    for folder in os.environ["MUSE_SOURCES"].split(os.pathsep):
        for path in pathlib.Path(folder).glob("*.pdf"):
            bars = decode(path).bars
            silent += [(path.stem, after.number) for before, after in zip(bars, bars[1:], strict=False) if before.length != after.length and after.time is None]
    assert not silent


def printed_line_starts(path: pathlib.Path) -> list[int]:
    """The bar numbers a source prints at the left of each line, in reading order."""
    import pymupdf

    found = []
    for at, page in enumerate(pymupdf.open(path)):
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                found += [(at, span["origin"][1], int(span["text"])) for span in line["spans"] if span["font"] == "Edwin-Italic" and span["text"].strip().isdigit() and span["origin"][0] < 60]
    return [number for _, _, number in sorted(found)]


def test_every_leland_source_starts_its_lines_on_the_printed_bar_numbers() -> None:
    from muse.song import encoding

    sources = [path for folder in os.environ["MUSE_SOURCES"].split(os.pathsep) for path in sorted(pathlib.Path(folder).glob("*.pdf")) if encoding(path) == "leland"]
    assert sources, "no Leland source found: the guard would judge nothing"
    wrong = {}
    for path in sources:
        printed = printed_line_starts(path)
        decoded = [bar.number for bar in decode(path).bars if bar.new_system]
        if not printed or decoded != printed:
            wrong[path.stem] = (printed, decoded)
    assert not wrong
