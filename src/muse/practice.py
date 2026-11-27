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


def final_score(work: pathlib.Path, name: str, variant: str, size: float) -> pathlib.Path:
    """Name 1 sheet's final score and refuse it when absent.

    Returns:
        The final score's path.

    Raises:
        FileNotFoundError: the final score is absent.

    """
    final = work / f"{name}-{variant}-{size:g}-final.mscz"
    if not final.exists():
        msg = f"{name}: no final score {final}"
        raise FileNotFoundError(msg)
    return final


def entries(song: Song, work: pathlib.Path, videos: pathlib.Path) -> list[dict[str, object]]:
    """Build 1 song's job entries: its favourite key, and its original key too when the favourite is transposed.

    Returns:
        The job entries, each naming its score, its video and any transpose options.

    """
    variant, size = status.favourite_size(song.name)
    built: list[dict[str, object]] = [
        {"in": str(final_score(work, song.name, variant, size)), "out": str(videos / f"{song.name}-{song.tone}.mp4")},
    ]
    options = port.transpose_options(song) if variant == "tone" else None
    if options:
        built[0]["transpose"] = options
        original = status.chosen_size(song.name, "original")
        built.append({
            "in": str(final_score(work, song.name, "original", original)),
            "out": str(videos / f"{song.name}-{song.source_key}.mp4"),
        })
    return built


def main() -> None:
    """Write 1 job for every named song and run it once through the app's practice view mode."""
    launch.ignore_terminal_stops()
    launch.require_mscore()
    if len(sys.argv) < ARGC:
        sys.stderr.write("usage: python -m muse.practice <sources joined with :> <work> <videos> <song>...\n")
        sys.exit(2)
    folders = [pathlib.Path(f) for f in sys.argv[1].split(":")]
    work, videos = pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3])
    names = sys.argv[4:]
    videos.mkdir(parents=True, exist_ok=True)
    made = [one for name in names for one in entries(song_of(find_source(folders, name)), work, videos)]
    with tempfile.TemporaryDirectory(prefix="practice-") as folder:
        job = pathlib.Path(folder) / "practice.json"
        job.write_text(json.dumps(made), encoding="utf-8")
        launch.heavy(
            [launch.mscore_command(), "--view-mode", "practice", "-j", str(job)],
            PRACTICE_SECONDS_PER_SONG * len(made),
            check=True,
            env=launch.mscore_environment(),
        )
    for one in made:
        sys.stdout.write(f"{one['out']}\n")


if __name__ == "__main__":
    main()
