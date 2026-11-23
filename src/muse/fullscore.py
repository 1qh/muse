"""An orchestral full score's files: its MuseScore file and its PDF.

Both are in the original key, broken where the source breaks.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import TYPE_CHECKING

from muse import chordnames, musicxml, pdftext, port
from muse.model import Orchestra, Score
from muse.song import Song, orchestra_of

if TYPE_CHECKING:
    import pathlib

SPATIUM = 0.9
NAMED: dict[str, object] = {
    "firstSystemInstNameVisibility": 0,
    "subsSystemInstNameVisibility": 1,
    "enableIndentationOnFirstSystem": 1,
    "enableVerticalSpread": 1,
    "maxSystemDistance": 200,
    "maxStaffSpread": 200,
    "maxSystemSpread": 200,
    "maxPageFillSpread": 400,
}
SPATIUM_STEP = 0.05
SPATIUM_FLOOR = 0.5
DISTANCE = port.BASE_DISTANCE


def xml_path(work: pathlib.Path, info: Song) -> pathlib.Path:
    return work / f"{info.name}-{port.ORIGINAL}.musicxml"


def breaks_of(orchestra: Orchestra) -> Score:
    """Take the source's system and page breaks, which every part shares, as the layout writer reads them.

    Returns:
        A score carrying the first part's bars.

    """
    return Score(orchestra.parts[0].bars, [], [])


def build(src: pathlib.Path, out_dir: pathlib.Path, work: pathlib.Path, info: Song) -> dict[str, object]:
    """Write the 2 files of 1 full score.

    Returns:
        The build report: PDF page count and the chosen staff space.

    """
    orchestra = orchestra_of(src)
    xml = xml_path(work, info)
    xml.write_text(musicxml.write_orchestra(orchestra), encoding="utf-8")
    final = out_dir / info.score_file
    spatium = fit(xml, work, final, info, orchestra)
    chordnames.drop_chord_path(final)
    pdf = out_dir / info.original_pdf
    port.run("-o", str(pdf), str(final))
    report: dict[str, object] = {"pdf pages": len(pdftext.open(pdf)), "spatium": spatium}
    return report


def system_problems(orchestra: Orchestra, mpos: pathlib.Path) -> list[str]:
    """Every system of the score opens on the bar its source system opens on.

    Returns:
        The mismatch, or an empty list.

    """
    boxes = [
        (int(float(e.get("page") or 0)), round(float(e.get("y") or 0))) for e in ET.parse(mpos).getroot().iter("element")
    ]
    starts = [at + 1 for at, box in enumerate(boxes) if not at or box != boxes[at - 1]]
    wanted = [1] + [bar.number for bar in orchestra.parts[0].bars if bar.new_system]
    return [] if starts == wanted else [f"systems open on bars {starts}, the source's on {wanted}"]


def fit(xml: pathlib.Path, work: pathlib.Path, final: pathlib.Path, info: Song, orchestra: Orchestra) -> float:
    """Find the largest staff space at which every source system holds on 1 line.

    It is judged on MuseScore's own layout and written to the final file.

    Returns:
        The staff space that fits.

    Raises:
        DoesNotFit: no staff space down to the floor holds every source system.

    """
    tried = []
    spatium = SPATIUM
    while spatium >= SPATIUM_FLOOR - 1e-9:
        style = port.styled(work, f"{info.name}-score-{spatium:g}.mss", info.title, spatium, DISTANCE, NAMED)
        raw = work / f"{info.name}-score-{spatium:g}-import.mscz"
        port.run("-S", str(style), "-o", str(raw), str(xml))
        port.strip_title_frame(raw)
        port.edited(raw, final, spatium, DISTANCE, breaks_of(orchestra))
        probe = work / f"{info.name}-score-{spatium:g}.mpos"
        port.run("-o", str(probe), str(final))
        problems = system_problems(orchestra, probe)
        if not problems:
            return spatium
        tried.append(f"{spatium:g}: {problems[0]}")
        spatium = round(spatium - SPATIUM_STEP, 2)
    msg = f"{info.name}: no staff space holds every source system: {tried[-3:]}"
    raise port.DoesNotFit(msg)
