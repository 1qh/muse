"""Which score each practice video is made from."""

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


def test_a_transposed_favourite_sheet_takes_its_own_final_score_and_the_song_transpose(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """A tone sheet is the tone size's final score moved by the song's own options."""
    monkeypatch.setattr(status, "favourite_size", lambda _name: ("tone", TONE_SIZE))
    final = tmp_path / "Song-tone-1.25-final.mscz"
    final.write_bytes(b"")
    made = practice.entry(song("down"), tmp_path, tmp_path / "videos")
    assert made["in"] == str(final)
    assert made["out"] == str(tmp_path / "videos" / "Song-G.mp4")
    transpose = made["transpose"]
    assert isinstance(transpose, dict)
    assert transpose["targetKey"] == G_FIFTHS
    assert transpose["direction"] == "down"


def test_an_original_favourite_sheet_is_never_transposed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """An original sheet that already is the favourite key goes as it is."""
    monkeypatch.setattr(status, "favourite_size", lambda _name: ("original", ORIGINAL_SIZE))
    (tmp_path / "Song-original-1.15-final.mscz").write_bytes(b"")
    assert "transpose" not in practice.entry(song("down"), tmp_path, tmp_path)


def test_a_missing_final_score_is_refused_by_its_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """A video is never made from another score."""
    monkeypatch.setattr(status, "favourite_size", lambda _name: ("tone", TONE_SIZE))
    with pytest.raises(FileNotFoundError, match=re.escape("Song-tone-1.25-final.mscz")):
        practice.entry(song("down"), tmp_path, tmp_path)
