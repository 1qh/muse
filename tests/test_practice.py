"""Which scores each song's practice videos are made from."""

import pathlib
import re

import pytest

from muse import practice, status
from muse.song import Song

TONE_SIZE = 1.25
ORIGINAL_SIZE = 1.15
G_FIFTHS = 1


def song(direction: str) -> Song:
    """Build a song moving from A to G, or staying put.

    Returns:
        The song.

    """
    return Song("Song", "A", 3, "G", G_FIFTHS, direction, -2, "Song")


def sizes(favourite: str) -> tuple[object, object]:
    """Fake the build records' size readers for a song whose favourite sheet is the given variant.

    Returns:
        The favourite reader and the per-variant reader.

    """
    chosen = {"tone": TONE_SIZE, "original": ORIGINAL_SIZE}

    def favourite_size(_name: str) -> tuple[str, float]:
        return favourite, chosen[favourite]

    def chosen_size(_name: str, variant: str) -> float:
        return chosen[variant]

    return favourite_size, chosen_size


def test_a_transposed_song_gets_its_favourite_key_and_its_original_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """A tone sheet is moved by the song's own options, and the original sheet goes as written beside it."""
    favourite_size, chosen_size = sizes("tone")
    monkeypatch.setattr(status, "favourite_size", favourite_size)
    monkeypatch.setattr(status, "chosen_size", chosen_size)
    tone = tmp_path / "Song-tone-1.25-final.mscz"
    original = tmp_path / "Song-original-1.15-final.mscz"
    tone.write_bytes(b"")
    original.write_bytes(b"")
    made = practice.entries(song("down"), tmp_path, tmp_path / "videos")
    assert [entry["in"] for entry in made] == [str(tone), str(original)]
    assert [entry["out"] for entry in made] == [
        str(tmp_path / "videos" / "Song-G.mp4"),
        str(tmp_path / "videos" / "Song-A.mp4"),
    ]
    transpose = made[0]["transpose"]
    assert isinstance(transpose, dict)
    assert transpose["targetKey"] == G_FIFTHS
    assert transpose["direction"] == "down"
    assert "transpose" not in made[1]


def test_a_song_already_in_its_favourite_key_gets_1_video(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """An original sheet that already is the favourite key goes as it is, once."""
    favourite_size, chosen_size = sizes("original")
    monkeypatch.setattr(status, "favourite_size", favourite_size)
    monkeypatch.setattr(status, "chosen_size", chosen_size)
    (tmp_path / "Song-original-1.15-final.mscz").write_bytes(b"")
    made = practice.entries(song("down"), tmp_path, tmp_path)
    assert len(made) == 1
    assert "transpose" not in made[0]


def test_a_missing_final_score_is_refused_by_its_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """A video is never made from another score."""
    favourite_size, chosen_size = sizes("tone")
    monkeypatch.setattr(status, "favourite_size", favourite_size)
    monkeypatch.setattr(status, "chosen_size", chosen_size)
    with pytest.raises(FileNotFoundError, match=re.escape("Song-tone-1.25-final.mscz")):
        practice.entries(song("down"), tmp_path, tmp_path)
