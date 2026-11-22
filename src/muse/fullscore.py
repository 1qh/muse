"""An orchestral full score's files: its MuseScore 4 file, its MuseScore 3 file and its PDF, in the original key, broken where the source breaks."""
from __future__ import annotations

import pathlib
import re
import xml.etree.ElementTree as ET
from fractions import Fraction as F

from muse import chordnames, launch, musicxml, pdftext, port, scorefile
from muse.model import Orchestra, Score
from muse.song import Song, orchestra_of

SPATIUM = 0.9  # millimetres: the source's own staff space, which holds its largest system of 23 staves on 1 page
NAMED: dict[str, object] = {
    "firstSystemInstNameVisibility": 0, "subsSystemInstNameVisibility": 1, "enableIndentationOnFirstSystem": 1,  # full names on the first system, short ones after, the first system indented to hold them
    "enableVerticalSpread": 1, "maxSystemDistance": 200, "maxStaffSpread": 200, "maxSystemSpread": 200,  # systems spread over the whole page height
    "maxPageFillSpread": 400,  # "Max page fill distance": the handbook says to raise it so staves are justified however few a page holds
}
SPATIUM_STEP = 0.05  # millimetres the staff space shrinks by while a source system wraps
SPATIUM_FLOOR = 0.5  # millimetres: the smallest staff space a full score prints at
DISTANCE = port.BASE_DISTANCE


def xml_path(work: pathlib.Path, info: Song) -> pathlib.Path:
    return work / f"{info.name}-{port.ORIGINAL}.musicxml"


def breaks_of(orchestra: Orchestra) -> Score:
    """The source's system and page breaks, which every part shares, as the layout writer reads them."""
    return Score(orchestra.parts[0].bars, [], [])


def build(src: pathlib.Path, out_dir: pathlib.Path, work: pathlib.Path, info: Song) -> dict[str, object]:
    """The 3 files of 1 full score; the MuseScore 3 file is withheld, with its reason, where it cannot match the latest."""
    orchestra = orchestra_of(src)
    xml = xml_path(work, info)
    xml.write_text(musicxml.write_orchestra(orchestra), encoding="utf-8")
    final = out_dir / info.score_file
    spatium = fit(xml, work, final, info, orchestra)
    chordnames.drop_chord_path(final)
    pdf = out_dir / info.original_pdf
    port.run("-o", str(pdf), str(final))
    report: dict[str, object] = {"pdf pages": len(pdftext.open(pdf)), "spatium": spatium}
    legacy = out_dir / info.legacy_file
    reason = build_legacy(xml, work, legacy, final, info, orchestra, spatium)
    if reason:
        legacy.unlink(missing_ok=True)
        report["legacy"] = reason
    return report


def system_problems(orchestra: Orchestra, mpos: pathlib.Path) -> list[str]:
    """Every system of the score opens on the bar its source system opens on."""
    boxes = [(int(float(e.get("page") or 0)), round(float(e.get("y") or 0))) for e in ET.parse(mpos).getroot().iter("element")]
    starts = [at + 1 for at, box in enumerate(boxes) if not at or box != boxes[at - 1]]
    wanted = [1] + [bar.number for bar in orchestra.parts[0].bars if bar.new_system]
    return [] if starts == wanted else [f"systems open on bars {starts}, the source's on {wanted}"]


def fit(xml: pathlib.Path, work: pathlib.Path, final: pathlib.Path, info: Song, orchestra: Orchestra) -> float:
    """The largest staff space at which every source system holds on 1 line, judged on MuseScore's own layout, written to the final file."""
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
    raise port.DoesNotFit(f"{info.name}: no staff space holds every source system: {tried[-3:]}")


def build_legacy(xml: pathlib.Path, work: pathlib.Path, target: pathlib.Path, latest: pathlib.Path, info: Song, orchestra: Orchestra, spatium: float) -> list[str]:
    """The MuseScore 3 file, from the pipeline's own MusicXML, in MuseScore 3.6.2; what keeps it from matching the latest file, or nothing."""
    style = port.styled(work, f"{info.name}-score-legacy.mss", info.title, spatium, DISTANCE, NAMED)  # the same size and named staves as the latest file
    fonts = "".join(f"<{tag}>SF Pro Display</{tag}>" for tag in port.LEGACY_ONLY_FONTS)
    text = style.read_text(encoding="utf-8").replace('version="4.70"', 'version="3.02"').replace("</Style>", fonts + "</Style>")
    style.write_text(re.sub(r"<staffUpperBorder>[^<]*</staffUpperBorder>", f"<staffUpperBorder>{port.LEGACY_TOP_BORDER}</staffUpperBorder>", text), encoding="utf-8")
    raw = work / f"{info.name}-score-legacy-import.mscz"
    plain = work / f"{info.name}-legacy.musicxml"  # MuseScore 3 stops reading a part at a harmonic: it gets them written into its file instead
    plain.write_text(xml.read_text(encoding="utf-8").replace(musicxml.TECHNICAL_XML["harmonic"], "").replace("<technical></technical>", ""), encoding="utf-8")
    done = launch.mscore(["-S", str(style), "-o", str(raw), str(plain)], port.LAUNCH_SECONDS, port.legacy_command())
    if done.returncode or not raw.exists():
        return [f"MuseScore 3 refused the MusicXML: exit {done.returncode}: {done.stderr.strip()[-300:]}"]
    port.strip_title_frame(raw)
    port.legacy_signs(raw)
    port.edited(raw, target, spatium, DISTANCE, breaks_of(orchestra))
    chordnames.drop_chord_path(target)
    lost = scorefile.articulate(target, harmonics(orchestra))
    return lost + scorefile.difference(latest, target)


def harmonics(orchestra: Orchestra) -> list[tuple[int, int, F, str]]:
    """Every harmonic as (staff index, bar, onset in quarters, MuseScore's articulation subtype)."""
    found = []
    first = 0
    for part in orchestra.parts:
        for bar in part.bars:
            for staff, voices in enumerate((bar.voices, bar.voices2)):
                found += [(first + staff, bar.number, event.onset, "stringsHarmonic") for voice in voices for event in voice if "harmonic" in event.marks]
        first += part.staves
    return found
