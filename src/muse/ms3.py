"""The MuseScore 3 file: a plain conversion of the pipeline's MusicXML, kept for playback with MuseScore 3's piano keyboard, and judged only on what it plays."""
from __future__ import annotations

import os
import pathlib
import zipfile

from muse import chordnames, launch, musicxml
from muse.song import Song, decode


def command() -> str:
    """MuseScore 3.7's own executable."""
    return os.environ.get("MUSE_MS3") or str(chordnames.MS3_APP / "Contents" / "MacOS" / "mscore")


def build(xml: pathlib.Path, target: pathlib.Path) -> None:
    """MuseScore 3.7 opens the MusicXML with its own defaults and saves it; only chord names it cannot parse are respelled, so no chord goes missing."""
    done = launch.mscore(["-o", str(target), str(xml)], launch.LAUNCH_SECONDS, command())
    if done.returncode or not target.exists():
        raise RuntimeError(f"MuseScore 3 refused {xml.name}: exit {done.returncode}: {done.stderr.strip()[-300:]}")
    respell(target)


def build_song(src: pathlib.Path, work: pathlib.Path, target: pathlib.Path, info: Song) -> None:
    """A lead sheet's MuseScore 3 file, from the same MusicXML writer the MuseScore 4 file is imported from."""
    xml = work / f"{info.name}-ms3.musicxml"
    xml.write_text(musicxml.write(decode(src)), encoding="utf-8")
    build(xml, target)


def respell(mscz: pathlib.Path) -> None:
    """Each chord name MuseScore 3 cannot parse written in the spelling it reads as the same chord."""
    with zipfile.ZipFile(mscz) as archive:
        items = {item.filename: archive.read(item.filename) for item in archive.infolist()}
    for name, data in items.items():
        if name.endswith(".mscx"):
            text = data.decode("utf-8")
            for spelled, ms3_name in chordnames.MS3_SPELLING.items():
                text = text.replace(f"<name>{spelled}</name>", f"<name>{ms3_name}</name>")
            items[name] = text.encode("utf-8")
    with zipfile.ZipFile(mscz, "w", zipfile.ZIP_DEFLATED) as archive:
        for key, data in items.items():
            archive.writestr(key, data)
