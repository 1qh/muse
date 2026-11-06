"""Calculated layout: measure a sheet's natural bar widths once, plan its lines in code, render the plan, and correct only what measurement shows off."""
from __future__ import annotations

import concurrent.futures
import os
import pathlib
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from typing import Any

from muse import chordseat, pdfcheck, pdftext, piano, port
from muse.model import Score
from muse.render import BATCHER
from muse.song import decode, song_of

LINE_SHARE = 0.93  # of a line's width a plan fills; the rest is the clef and key each line carries
WIDE_PAGE = 200.0  # inches: wide enough that a whole song prints on 1 line at its natural width
WRAP_ROUNDS = 6
SPLIT_ROUNDS = 3
INK_ROUNDS = 3
EXTENSION = pathlib.Path(__file__).parent / "extension"
EXTENSION_URI = "extension://muse/seat"
EXTENSION_SECONDS = 300


def extension_home() -> pathlib.Path:
    """Where MuseScore reads user extensions; MUSE_EXTENSIONS overrides it."""
    given = os.environ.get("MUSE_EXTENSIONS")
    return pathlib.Path(given) if given else pathlib.Path.home() / "Library" / "Application Support" / "MuseScore" / "MuseScore4" / "extensions" / "museseat"


def install_extension() -> None:
    """Copy the pipeline's own extension where MuseScore reads it, so no build depends on a hand-installed copy."""
    home = extension_home()
    home.mkdir(parents=True, exist_ok=True)
    for part in EXTENSION.iterdir():
        if part.is_file() and (not (home / part.name).exists() or (home / part.name).read_bytes() != part.read_bytes()):
            shutil.copyfile(part, home / part.name)


def run_extension(source: pathlib.Path, target: pathlib.Path) -> None:
    """1 MuseScore launch: open the score, seat its chords by measuring its own layout, save it."""
    port.LAUNCHES["count"] += 1
    done = subprocess.run(["mscore", "--extension", EXTENSION_URI, "-o", str(target), str(source)], capture_output=True, text=True, timeout=EXTENSION_SECONDS, stdin=subprocess.DEVNULL, check=False)
    if done.returncode or not target.exists():
        raise port.DoesNotFit(f"{source.name}: the seating extension failed: {done.stderr[-300:]}")


POINTS_PER_MM = 72 / 25.4
CLEAR_SPACES = port.CHORD_GAP * port.CHORD_SIZE_PER_SPACE / POINTS_PER_MM  # the clear gap between 2 chords, in staff spaces at any size


def set_tags(score_path: pathlib.Path, tags: dict[str, str]) -> None:
    """Write score tags the extension reads, replacing any earlier value of each."""

    def change(name: str, text: str) -> str:
        if not name.endswith(".mscx"):
            return text
        for key, value in tags.items():
            text = re.sub(rf'\s*<metaTag name="{key}">[^<]*</metaTag>', "", text)
            text = text.replace("<Score>", f'<Score>\n    <metaTag name="{key}">{value}</metaTag>', 1)
        return text

    rewrite(score_path, change)


def read_tag(score_path: pathlib.Path, key: str) -> str:
    """A score tag's value; DoesNotFit when the extension never wrote it."""
    _, text = chordseat.score_text(score_path)
    found = re.search(rf'<metaTag name="{key}">([^<]*)</metaTag>', text)
    if found is None:
        raise port.DoesNotFit(f"{score_path.name}: the extension wrote no {key}")
    return found.group(1)


@dataclass
class Sheet:
    """1 sheet of a song at 1 size: its score with line and page breaks, and where it renders."""

    src: pathlib.Path
    work: pathlib.Path
    variant: str
    size: float
    distance: float = port.BASE_DISTANCE
    score: Score = field(init=False)
    title: str = field(init=False)
    options: dict[str, Any] | None = field(init=False)
    style: pathlib.Path = field(init=False)
    name: str = field(init=False)

    def __post_init__(self) -> None:
        self.score = decode(self.src)
        info = song_of(self.src, self.score)
        if self.variant == port.PIANO:
            piano.reduce(self.score)
        port.lift_idle_cues(self.score)
        self.title = info.title
        self.options = None if self.variant == port.ORIGINAL else port.transpose_options(info)
        self.name = f"{info.name}-{self.variant}"
        self.style = port.styled(self.work, f"{self.name}-{self.size:g}.mss", info.title, self.size, self.distance)

    def path(self, label: str) -> pathlib.Path:
        return self.work / f"{self.name}-{self.size:g}-{label}.mscz"

    def imported(self) -> pathlib.Path:
        return port.imported(self.score, self.work, self.name, self.style, False)

    def starts(self) -> list[int]:
        """1-based bars that open a line."""
        return [1] + [bar.number for bar in self.score.bars[1:] if bar.new_system]

    def set_lines(self, starts: set[int]) -> None:
        for bar in self.score.bars:
            bar.new_system = bar.number in starts and bar.number != 1
            bar.new_page = False


def rewrite(path: pathlib.Path, change: Any) -> None:
    """Apply a text change to every score and style part of an .mscz."""
    with zipfile.ZipFile(path) as old:
        items = {name: old.read(name) for name in old.namelist()}
    for name, data in items.items():
        if name.endswith((".mss", ".mscx")):
            items[name] = change(name, data.decode("utf-8")).encode("utf-8")
    fresh = path.with_name(path.stem + "-rewrite.mscz")
    with zipfile.ZipFile(fresh, "w", zipfile.ZIP_DEFLATED) as new:
        for name, data in items.items():
            new.writestr(name, data)
    fresh.replace(path)


def unjustified(name: str, text: str) -> str:
    """The page so wide and the last line so unjustified that every bar prints at its natural width."""
    text = re.sub(r"<pageWidth>[^<]*</pageWidth>", f"<pageWidth>{WIDE_PAGE}</pageWidth>", text)
    text = re.sub(r"<pagePrintableWidth>[^<]*</pagePrintableWidth>", f"<pagePrintableWidth>{WIDE_PAGE - 0.5}</pagePrintableWidth>", text)
    text = re.sub(r"<lastSystemFillLimit>[^<]*</lastSystemFillLimit>", "", text)
    return text.replace("</Style>", "<lastSystemFillLimit>1</lastSystemFillLimit></Style>", 1) if "</Style>" in text else text


def placed(name: str, text: str) -> str:
    """Every chord symbol placed by code alone, so it never sets a bar's width or gets moved."""
    if not name.endswith(".mscx"):
        return text
    return re.sub(r"<Harmony>(?!<autoplace>)", "<Harmony><autoplace>0</autoplace>", text)


def boxes(mpos: pathlib.Path) -> list[dict[str, float]]:
    return [{key: float(element.get(key) or 0) for key in ("x", "y", "sx", "page")} for element in ET.parse(mpos).getroot().iter("element")]


def measure(sheet: Sheet) -> tuple[list[float], float]:
    """Each bar's natural width, and the room 1 line holds, in the program's own units."""
    flags = [(bar.new_system, bar.new_page) for bar in sheet.score.bars]
    sheet.set_lines({1})
    natural, normal = sheet.path("natural"), sheet.path("normal")
    for target in (natural, normal):
        port.edited(sheet.imported(), target, sheet.size, sheet.distance, sheet.score)
        rewrite(target, placed)
    rewrite(natural, unjustified)
    for bar, (line, page) in zip(sheet.score.bars, flags, strict=True):
        bar.new_system, bar.new_page = line, page
    natural_pdf = natural.with_suffix(".pdf")
    BATCHER.render(natural, [natural.with_suffix(".mpos"), natural_pdf], sheet.options)
    BATCHER.render(normal, [normal.with_suffix(".mpos")], sheet.options)
    laid_natural = boxes(natural.with_suffix(".mpos"))
    shares = chord_shares(natural, len(laid_natural))
    widths = [box["sx"] * share for box, share in zip(laid_natural, shares, strict=True)]
    laid = boxes(normal.with_suffix(".mpos"))
    first = [box for box in laid if box["page"] == laid[0]["page"] and box["y"] == laid[0]["y"]]
    return widths, sum(box["sx"] for box in first)


def chord_shares(natural: pathlib.Path, bars: int) -> list[float]:
    """Per bar, the share its natural width must grow to hold its chords apart by the clear gap, measured by the extension."""
    probe = natural.with_name(natural.stem + "-chords.mscz")
    shutil.copyfile(natural, probe)
    set_tags(probe, {"museMode": "measure", "museChordGap": f"{CLEAR_SPACES:.4f}"})
    run_extension(probe, probe)
    shares = [float(value) for value in read_tag(probe, "museChordShare").split(",")]
    if len(shares) != bars:
        raise port.DoesNotFit(f"{natural.name}: the extension measured {len(shares)} bars of {bars}")
    return shares


def plan(score: Score, widths: list[float], room: float) -> set[int]:
    """1-based line starts: each section block packed to a share of the line, a tied bar kept with its tie, no stranded last bar, no line opening without its chord."""
    cap = room * LINE_SHARE
    tied = [port.tied_out(bar) for bar in score.bars]
    starts: set[int] = set()
    empty = [not piano.has_notes(bar) for bar in score.bars]

    def cost(line: list[int]) -> float:
        """A line's width once its empty bars are evened to the widest of them, as they will be."""
        widest = max((widths[i] for i in line if empty[i]), default=0.0)
        return sum(widest if empty[i] else widths[i] for i in line)

    for block in port.sections(score):
        lines: list[list[int]] = [[block[0]]]
        for index in block[1:]:
            if cost([*lines[-1], index]) > cap and not tied[index - 1]:
                lines.append([index])
            else:
                lines[-1].append(index)
        if len(lines) > 1 and len(lines[-1]) == 1 and len(lines[-2]) > 2:  # a stranded last bar takes 1 from the line above
            lines[-1].insert(0, lines[-2].pop())
        starts |= {line[0] for line in lines}
    chorded = {index for index, bar in enumerate(score.bars) if bar.chords}
    fixed = {block[0] for block in port.sections(score)}
    settled = port.settle_chord_starts(starts, chorded, fixed, len(score.bars))
    return {index + 1 for index in settled}


def render(sheet: Sheet, label: str, seated: bool = False) -> tuple[pathlib.Path, pathlib.Path]:
    """The sheet as its score now breaks it; a seated render keeps the chord offsets already written."""
    score_path = sheet.path(label)
    if not seated or not score_path.exists():
        port.edited(sheet.imported(), score_path, sheet.size, sheet.distance, sheet.score)
        rewrite(score_path, placed)
    probe = score_path.with_name(score_path.stem + "-layout.pdf")
    BATCHER.render(score_path, [probe, probe.with_suffix(".mpos")], sheet.options)
    return score_path, probe


def candidate(src: pathlib.Path, work: pathlib.Path, variant: str, size: float) -> dict[str, Any]:
    """1 size planned and rendered once: whether its lines held, how full its pages are, and its short-line share."""
    with BATCHER.worker():
        sheet = Sheet(src, work, variant, size)
        widths, room = measure(sheet)
        sheet.set_lines(plan(sheet.score, widths, room))
        _, probe = render(sheet, "plan")
        wanted = sheet.starts()
        held = sorted({1, *port.system_starts(probe)}) == wanted
        lines = port.rows(sheet.score)
        good = held  # chords are seated, and so judged for crowding, only on the size that is finished
        return {"size": size, "held": held, "share": port.short_line_share(lines), "fill": port.page_fill(probe), "good": good,
                "lone": bool(piano.lone_lines(wanted, len(sheet.score.bars))) if variant == port.PIANO else False}


def choose(found: list[dict[str, Any]], variant: str) -> list[float]:
    """Sizes to finish, best first: the largest whose lines fill their pages within the base short-line share, then within the wider share."""
    order: list[float] = []
    for share in (port.MAX_SHORT_SHARE, port.RELAXED_SHORT_SHARE):
        fit = [c for c in found if c["good"] and not c["lone"] and (variant == port.PIANO or c["share"] <= share)]
        full = sorted((c["size"] for c in fit if c["fill"] >= port.PAGE_FILL), reverse=True)
        rest = sorted((c["size"] for c in fit if c["fill"] < port.PAGE_FILL), reverse=True)
        order += [size for size in full + rest if size not in order]
    return order


def correct_lines(sheet: Sheet) -> pathlib.Path:
    """Render the plan; a line the program wrapped hands its last bar down, until every line holds."""
    for _ in range(WRAP_ROUNDS):
        _, probe = render(sheet, "final")
        wanted, got = sheet.starts(), sorted({1, *port.system_starts(probe)})
        extra = sorted(set(got) - set(wanted))
        if not extra:  # crowding is judged once chords are seated, never here
            return probe
        starts = set(wanted)
        for row in port.rows(sheet.score):
            if any(row[0] < bar <= row[-1] for bar in extra) and len(row) > 1:
                starts.add(row[-1])
        sheet.set_lines(starts)
    raise port.DoesNotFit(f"{sheet.name} at {sheet.size}: lines still wrap")


def seat(sheet: Sheet, score_path: pathlib.Path, probe: pathlib.Path) -> pathlib.Path:
    """The extension fits each bar's chords, evens empty bars and seats chords; each round corrects every chord by its own measured ink offset."""
    bias: list[float] = []
    for _ in range(INK_ROUNDS):
        set_tags(score_path, {"museChordGap": f"{CLEAR_SPACES:.4f}", "museInkBias": ",".join(f"{b:.4f}" for b in bias)})
        run_extension(score_path, score_path)
        _, probe = render(sheet, "final", seated=True)
        if sheet.starts() != sorted({1, *port.system_starts(probe)}):
            return probe  # widening wrapped a line; the caller moves its last bar down
        _, text = chordseat.score_text(score_path)
        rows = chordseat.rows_of(text)
        lines = chordseat.matched(text, probe)
        gaps = chordseat.gaps(rows, lines)
        spaces = chordseat.spaces_of(rows, lines)
        bias = [(bias[at] if at < len(bias) else 0.0) + (gap or 0.0) / space for at, (gap, space) in enumerate(zip(gaps, spaces, strict=True))]
        if all(gap is None or abs(gap - chordseat.GAP) <= chordseat.TOLERANCE for gap in gaps):
            break
    return probe


def finish(src: pathlib.Path, work: pathlib.Path, variant: str, size: float) -> tuple[Score, pathlib.Path, float, float, int]:
    """1 size from plan to a seated sheet that passes every final check; DoesNotFit when it cannot."""
    sheet = Sheet(src, work, variant, size)
    widths, room = measure(sheet)
    sheet.set_lines(plan(sheet.score, widths, room))
    probe = correct_lines(sheet)
    if port.balance(sheet.score, probe):
        probe = correct_lines(sheet)
    score_path = sheet.path("final")
    for _ in range(SPLIT_ROUNDS):  # evening bars or seating chords can tip a line the plan judged clear
        probe = seat(sheet, score_path, probe)
        wanted, got = sheet.starts(), sorted({1, *port.system_starts(probe)})
        crowded = port.crowded_rows(probe, wanted) if wanted == got else set()
        tipped = {row[-1] for row in port.rows(sheet.score) if len(row) > 1 and (row[0] in crowded or any(row[0] < bar <= row[-1] for bar in set(got) - set(wanted)))}
        if os.environ.get("MUSE_LAYOUT_TRACE"):
            print(f"{sheet.name} {size}: wanted {wanted} got {got} crowded {sorted(crowded)} tipped {sorted(tipped)}", flush=True)
        if not tipped:
            break
        sheet.set_lines(set(wanted) | tipped)
        probe = correct_lines(sheet)
    else:
        raise port.DoesNotFit(f"{sheet.name} at {size}: lines still tip after seating")
    problems = pdfcheck.all_problems(probe, sheet.title) + chordseat.problems(score_path, probe) + port.line_rule_breaks(sheet.score)
    if problems:
        raise port.DoesNotFit(f"{sheet.name} at {size}: {problems[:3]}")
    return sheet.score, score_path, size, sheet.distance, len(pdftext.open(probe))


def lay(src: pathlib.Path, work: pathlib.Path, variant: str) -> tuple[Score, pathlib.Path, float, float, int]:
    """Every candidate size planned side by side in shared jobs; the best is finished, the next only if it cannot be."""
    install_extension()
    sizes = port.sizes_between(port.SIZE_FLOORS[-1], port.SIZE_CAP)
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(sizes)) as pool:
        found = list(pool.map(lambda size: candidate(src, work, variant, size), sizes))
    refusals = []
    for size in choose(found, variant):
        try:
            return finish(src, work, variant, size)
        except port.DoesNotFit as error:
            refusals.append(str(error))
    raise port.DoesNotFit(f"{src.stem} {variant}: no size passes: {refusals[:3]}")

