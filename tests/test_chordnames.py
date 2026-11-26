"""Chord name checks: where each check writes."""

import pathlib
from collections.abc import Callable

import pytest

from muse import chordnames


class _StopError(Exception):
    """Raised by the fake run once it has recorded where it was asked to write."""


@pytest.mark.parametrize("check", [chordnames.playback_problems, chordnames.transposes])
def test_each_chord_check_writes_in_its_own_folder_and_removes_it(
    check: Callable[[str, pathlib.Path, pathlib.Path, chordnames.Runner], list[str]],
    tmp_path: pathlib.Path,
) -> None:
    """2 songs checking at once never share a file, and no check leaves a folder behind."""
    folders: list[pathlib.Path] = []

    def run(arguments: list[str]) -> None:
        folders.append(pathlib.Path(arguments[-2] if arguments[1] == "-S" else arguments[2]).parent)
        raise _StopError

    for _ in range(2):
        with pytest.raises(_StopError):
            check("mscore", tmp_path / "style.mss", tmp_path, run)
    assert folders[0] != folders[1]
    assert all(not folder.exists() for folder in folders)
