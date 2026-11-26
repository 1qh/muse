"""Export each song's practice video through MuseScoreLite's own practice view mode, from its favourite-key final score.

usage: python -m muse.practice <sources joined with :> <work> <videos> <song>...
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
from typing import TYPE_CHECKING

from muse import launch, port, status
from muse.song import song_of

if TYPE_CHECKING:
    from muse.song import Song

PRACTICE_SECONDS_PER_SONG = 600
ARGC = 5


def find_source(folders: list[pathlib.Path], name: str) -> pathlib.Path:
    """Find a song's source PDF.

    Returns:
        The source PDF named for the song.

    Raises:
        FileNotFoundError: no folder holds a source for the song.

    """
    for folder in folders:
        found = sorted(folder.glob(f"{name}-*.pdf"))
        if found:
            return found[0]
    msg = f"{name}: no source in {[str(f) for f in folders]}"
    raise FileNotFoundError(msg)


def entry(song: Song, work: pathlib.Path, videos: pathlib.Path) -> dict[str, object]:
    """Build 1 song's job entry: its favourite-key final score, transposed when that sheet is the transposed one.

    Returns:
        The job entry naming the score, the video and any transpose options.

    Raises:
        FileNotFoundError: the favourite sheet's final score is absent.

    """
    variant, size = status.favourite_size(song.name)
    final = work / f"{song.name}-{variant}-{size:g}-final.mscz"
    if not final.exists():
        msg = f"{song.name}: no final score {final}"
        raise FileNotFoundError(msg)
    built: dict[str, object] = {"in": str(final), "out": str(videos / f"{song.name}-{song.tone}.mp4")}
    options = port.transpose_options(song) if variant == "tone" else None
    if options:
        built["transpose"] = options
    return built


def main() -> None:
    """Write 1 job for every named song and run it once through the app's practice view mode."""
    if len(sys.argv) < ARGC:
        sys.stderr.write("usage: python -m muse.practice <sources joined with :> <work> <videos> <song>...\n")
        sys.exit(2)
    folders = [pathlib.Path(f) for f in sys.argv[1].split(":")]
    work, videos = pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3])
    names = sys.argv[4:]
    videos.mkdir(parents=True, exist_ok=True)
    entries = [entry(song_of(find_source(folders, name)), work, videos) for name in names]
    with tempfile.TemporaryDirectory(prefix="practice-") as folder:
        job = pathlib.Path(folder) / "practice.json"
        job.write_text(json.dumps(entries), encoding="utf-8")
        launch.heavy(
            [launch.mscore_command(), "--view-mode", "practice", "-j", str(job)],
            PRACTICE_SECONDS_PER_SONG * len(entries),
            check=True,
            env=launch.mscore_environment(),
        )
    for made in entries:
        sys.stdout.write(f"{made['out']}\n")


if __name__ == "__main__":
    main()
