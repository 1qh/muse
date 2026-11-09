"""An orchestral full score's files: its MuseScore 4 file, its MuseScore 3 file and its PDF, in the original key, broken where the source breaks."""
from __future__ import annotations

import pathlib
import re

from muse import chordnames, launch, musicxml, pdftext, port, scorefile
from muse.model import Orchestra, Score
from muse.song import Song, orchestra_of

SPATIUM = 0.9  # millimetres: the source's own staff space, which holds its largest system of 23 staves on 1 page
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
    style = port.styled(work, f"{info.name}-score.mss", info.title, SPATIUM, DISTANCE)
    raw = work / f"{info.name}-score-import.mscz"
    port.run("-S", str(style), "-o", str(raw), str(xml))
    port.strip_title_frame(raw)
    final = out_dir / info.score_file
    port.edited(raw, final, SPATIUM, DISTANCE, breaks_of(orchestra))
    chordnames.drop_chord_path(final)
    pdf = out_dir / info.original_pdf
    port.run("-o", str(pdf), str(final))
    report: dict[str, object] = {"pdf pages": len(pdftext.open(pdf))}
    legacy = out_dir / info.legacy_file
    reason = build_legacy(xml, work, legacy, final, info, orchestra)
    if reason:
        legacy.unlink(missing_ok=True)
        report["legacy"] = reason
    return report


def build_legacy(xml: pathlib.Path, work: pathlib.Path, target: pathlib.Path, latest: pathlib.Path, info: Song, orchestra: Orchestra) -> list[str]:
    """The MuseScore 3 file, from the pipeline's own MusicXML, in MuseScore 3.6.2; what keeps it from matching the latest file, or nothing."""
    style = port.styled(work, f"{info.name}-score-legacy.mss", info.title, SPATIUM, DISTANCE)
    fonts = "".join(f"<{tag}>SF Pro Display</{tag}>" for tag in port.LEGACY_ONLY_FONTS)
    text = style.read_text(encoding="utf-8").replace('version="4.70"', 'version="3.02"').replace("</Style>", fonts + "</Style>")
    style.write_text(re.sub(r"<staffUpperBorder>[^<]*</staffUpperBorder>", f"<staffUpperBorder>{port.LEGACY_TOP_BORDER}</staffUpperBorder>", text), encoding="utf-8")
    raw = work / f"{info.name}-score-legacy-import.mscz"
    done = launch.mscore(["-S", str(style), "-o", str(raw), str(xml)], port.LAUNCH_SECONDS, port.legacy_command())
    if done.returncode or not raw.exists():
        return [f"MuseScore 3 refused the MusicXML: exit {done.returncode}: {done.stderr.strip()[-300:]}"]
    port.strip_title_frame(raw)
    port.legacy_signs(raw)
    port.edited(raw, target, SPATIUM, DISTANCE, breaks_of(orchestra))
    chordnames.drop_chord_path(target)
    return scorefile.difference(latest, target)
