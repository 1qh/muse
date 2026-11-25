"""Calculated layout: measure a sheet's natural bar widths once and plan its lines in code.

The plan is rendered, and only what measurement shows off is corrected.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import pathlib
import re
import shutil
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, TypedDict

from muse import chordseat, launch, mode, pdfcheck, pdftext, piano, port
from muse.render import BATCHER
from muse.song import decode, song_of


class Candidate(TypedDict):
    size: float
    held: bool
    share: float
    fill: float
    good: bool
    lines: int
    lone: bool


if TYPE_CHECKING:
    from collections.abc import Callable

    from muse.model import Score

LINE_SHARE = 0.93
WIDE_PAGE = 200.0
STALL_ROUNDS = 6
BALANCE_ROUNDS = 3
IMPORT_SIZE = 1.5
OVERFLOW_BREAKS = 1000
SHORT_COST = 0.15
SHORT_LINE_BAR_COUNT = 2
GROW = 1.06
GAP_REACH = 20.0
GAP_STEP = 2.0
GROW_STEPS = 40
INK_ROUNDS = 6
EXTENSION = pathlib.Path(__file__).parent / "extension"
EXTENSION_URI = "extension://muse/seat"
EXTENSION_FOLDER = "museseat"
INSTALL_LOCK = threading.Lock()
EXTENSION_SECONDS = 300
TRACE_VARIABLE = "MUSE_LAYOUT_TRACE"


def extensions_root() -> pathlib.Path:
    """Where MuseScore reads user extensions; MUSE_EXTENSIONS overrides it.

    Returns:
        The extensions folder.

    """
    given = os.environ.get("MUSE_EXTENSIONS")
    return pathlib.Path(given) if given is not None and len(given) > 0 else launch.APP_DATA / "extensions"


def install_extension() -> str:
    """Install the pipeline's own extension under a name carrying a digest of its bytes.

    2 versions never share 1 copy.

    Returns:
        The address to run the installed extension by.

    """
    script = (EXTENSION / "main.js").read_bytes()
    digest = hashlib.sha256(script + (EXTENSION / "manifest.json").read_bytes()).hexdigest()[:12]
    uri = f"{EXTENSION_URI}-{digest}"
    home = extensions_root() / f"{EXTENSION_FOLDER}-{digest}"
    with INSTALL_LOCK:
        if (home / "manifest.json").exists():
            return uri
        manifest = json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))
        manifest["uri"] = uri
        fresh = home.with_name(f"{home.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        shutil.rmtree(fresh, ignore_errors=True)
        fresh.mkdir(parents=True)
        (fresh / "main.js").write_bytes(script)
        (fresh / "manifest.json").write_text(json.dumps(manifest, indent=4), encoding="utf-8")
        try:
            fresh.rename(home)
        except OSError:
            shutil.rmtree(fresh, ignore_errors=True)
        shutil.rmtree(extensions_root() / EXTENSION_FOLDER, ignore_errors=True)
    return uri


def run_extension(source: pathlib.Path, target: pathlib.Path) -> None:
    """1 MuseScore launch: open the score, seat its chords by measuring its own layout, save it.

    Raises:
        DoesNotFitError: when the launch fails or writes no target.

    """
    uri = install_extension()
    if target != source:
        target.unlink(missing_ok=True)
    written = target.stat().st_mtime_ns if target.exists() else None
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as handle:
        json.dump([{"in": str(source), "out": str(target)}], handle)
        job = pathlib.Path(handle.name)
    try:
        note = launch.served(job, EXTENSION_SECONDS, extension=uri)
    finally:
        job.unlink(missing_ok=True)
    if note or not target.exists() or target.stat().st_mtime_ns == written:
        msg = f"{source.name}: the seating extension failed: {note[-300:]}"
        raise port.DoesNotFitError(msg)


POINTS_PER_MM = 72 / 25.4
CLEAR_SPACES = port.CHORD_GAP * port.CHORD_SIZE_PER_SPACE / POINTS_PER_MM


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
    """Read a score tag's value; DoesNotFitError when the extension never wrote it.

    Returns:
        The tag's value.

    Raises:
        DoesNotFitError: when the extension never wrote the tag.

    """
    _, text = chordseat.score_text(score_path)
    found = re.search(rf'<metaTag name="{key}">([^<]*)</metaTag>', text)
    if found is None:
        msg = f"{score_path.name}: the extension wrote no {key}"
        raise port.DoesNotFitError(msg)
    return str(found.group(1))


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
    options: port.TransposeOptions | None = field(init=False)
    style: pathlib.Path = field(init=False)
    import_style: pathlib.Path = field(init=False)
    name: str = field(init=False)
    shares: list[float] = field(init=False, default_factory=list)

    def __post_init__(self) -> None:
        self.score = decode(self.src)
        info = song_of(self.src, self.score)
        if self.variant == port.PIANO:
            piano.reduce(self.score)
        port.lift_idle_cues(self.score)
        self.title = info.title
        self.options = None if self.variant == port.ORIGINAL else port.transpose_options(info)
        self.name = f"{info.name}-{self.variant}"
        self.style = port.styled(
            self.work,
            f"{self.name}-{self.size:g}.mss",
            info.title,
            port.Sizing(self.size, self.distance),
        )
        self.import_style = port.styled(
            self.work,
            f"{self.name}-import.mss",
            info.title,
            port.Sizing(IMPORT_SIZE, self.distance),
        )

    def path(self, label: str) -> pathlib.Path:
        return self.work / f"{self.name}-{self.size:g}-{label}.mscz"

    def imported(self) -> pathlib.Path:
        return port.imported(self.score, self.work, self.name, self.import_style, clear=False)

    def starts(self) -> list[int]:
        """1-based bars that open a line.

        Returns:
            The bar numbers that open a line.

        """
        return [1] + [bar.number for bar in self.score.bars[1:] if bar.new_system]

    def set_lines(self, starts: set[int]) -> None:
        for bar in self.score.bars:
            bar.new_system = bar.number in starts and bar.number != 1
            bar.new_page = False


def rewrite(path: pathlib.Path, change: Callable[[str, str], str]) -> None:
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
    """Make the page so wide and the last line so unjustified that every bar prints at its natural width.

    Returns:
        The changed text.

    """
    del name
    text = re.sub(r"<pageWidth>[^<]*</pageWidth>", f"<pageWidth>{WIDE_PAGE}</pageWidth>", text)
    text = re.sub(
        r"<pagePrintableWidth>[^<]*</pagePrintableWidth>",
        f"<pagePrintableWidth>{WIDE_PAGE - 0.5}</pagePrintableWidth>",
        text,
    )
    text = re.sub(r"<lastSystemFillLimit>[^<]*</lastSystemFillLimit>", "", text)
    return (
        text.replace("</Style>", "<lastSystemFillLimit>1</lastSystemFillLimit></Style>", 1) if "</Style>" in text else text
    )


def placed(name: str, text: str) -> str:
    """Every chord symbol placed by code alone, so it never sets a bar's width or gets moved.

    Returns:
        The changed text.

    """
    if not name.endswith(".mscx"):
        return text
    return re.sub(r"<Harmony>(?!<autoplace>)", "<Harmony><autoplace>0</autoplace>", text)


def boxes(mpos: pathlib.Path) -> list[dict[str, float]]:
    return [
        {key: float(element.get(key) or 0) for key in ("x", "y", "sx", "page")}
        for element in ET.parse(mpos).getroot().iter("element")
    ]


def measure(sheet: Sheet) -> tuple[list[float], float]:
    """Each bar's natural width, and the room 1 line holds, in the program's own units.

    Returns:
        The natural widths and the room of 1 line.

    """
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
    shares = chord_shares(
        natural,
        len(laid_natural),
        [index for index, bar in enumerate(sheet.score.bars) if piano.has_notes(bar)],
    )
    sheet.shares = shares
    widths = [box["sx"] * share for box, share in zip(laid_natural, shares, strict=True)]
    laid = boxes(normal.with_suffix(".mpos"))
    first = [box for box in laid if box["page"] == laid[0]["page"] and box["y"] == laid[0]["y"]]
    return widths, sum(box["sx"] for box in first)


def chord_shares(natural: pathlib.Path, bars: int, noted: list[int]) -> list[float]:
    """Per bar, the share its natural width must grow to hold its chords apart by the clear gap.

    The extension measures it.

    Returns:
        Each bar's share.

    Raises:
        DoesNotFitError: when the extension reports crowding or measures the wrong bar count.

    """
    probe = natural.with_name(natural.stem + "-chords.mscz")
    shutil.copyfile(natural, probe)
    set_tags(
        probe,
        {
            "museMode": "measure",
            "museChordGap": f"{CLEAR_SPACES:.4f}",
            "museNoted": ",".join(str(i) for i in noted) or "-1",
        },
    )
    run_extension(probe, probe)
    crowded = read_tag(probe, "museSeatError")
    if crowded:
        msg = f"{natural.name}: measuring chord room: {crowded}"
        raise port.DoesNotFitError(msg)
    shares = [float(value) for value in read_tag(probe, "museChordShare").split(",")]
    if len(shares) != bars:
        msg = f"{natural.name}: the extension measured {len(shares)} bars of {bars}"
        raise port.DoesNotFitError(msg)
    return shares


def blocks_of(score: Score, variant: str | None) -> list[list[int]]:
    """0-based bars of each section; a section of 1 bar joins the block after it.

    That block's mark or label then sits second rather than the bar standing alone.

    Returns:
        The bars of each block.

    """
    del variant
    blocks = [list(block) for block in port.sections(score)]
    joined: list[list[int]] = []
    for block in blocks:
        previous = joined[-1] if joined else None
        if previous is not None and len(previous) == 1:
            previous.extend(block)
        else:
            joined.append(block)
    return joined


def plan(score: Score, widths: list[float], room: float, variant: str | None = None) -> set[int]:
    """1-based line starts: per section, the break set with fewest rule breaks.

    Ties go to fewest lines, then the most even fill.

    Returns:
        The 1-based bars that open a line.

    """
    cap = room * LINE_SHARE
    tied = [port.tied_out(bar) for bar in score.bars]
    empty = [not piano.has_notes(bar) for bar in score.bars]
    chorded = [bool(bar.chords) or bar.source_line for bar in score.bars]

    def width(line: list[int]) -> float:
        """Measure a line's width once its empty bars are evened to the widest of them, as they will be.

        An empty bar widens for its own chords and its line's empty bars follow.

        Returns:
            The line's width.

        """
        widest = max((widths[i] for i in line if empty[i]), default=0.0)
        return sum(widest if empty[i] else widths[i] for i in line)

    def judged(line: list[int], *, opens_block: bool, ends_block: bool, lone_allowed: bool) -> tuple[int, float]:
        used = width(line)
        lone = len(line) == 1 and not lone_allowed
        breaks = (
            OVERFLOW_BREAKS * (used > cap and len(line) > 1)
            + lone
            + (not opens_block and not chorded[line[0]])
            + (not ends_block and tied[line[-1]])
        )
        cost = ((cap - used) / cap) ** 2 if used <= cap else 0.0
        if len(line) == SHORT_LINE_BAR_COUNT:
            cost += SHORT_COST
        return breaks, cost

    starts: set[int] = set()
    for block in blocks_of(score, variant):
        count = len(block)
        best: dict[int, tuple[int, int, float, int]] = {0: (0, 0, 0.0, -1)}
        for end in range(1, count + 1):
            for begin in range(end):
                before = best.get(begin)
                if before is None:
                    continue
                breaks, cost = judged(
                    block[begin:end],
                    opens_block=begin == 0,
                    ends_block=end == count,
                    lone_allowed=count == 1,
                )
                option = (before[0] + breaks, before[1] + 1, before[2] + cost, begin)
                current = best.get(end)
                if current is None or option[:3] < current[:3]:
                    best[end] = option
        at = count
        while at > 0:
            chosen = best[at]
            starts.add(block[chosen[3]])
            at = chosen[3]
    return {index + 1 for index in starts}


def render(sheet: Sheet, label: str, *, seated: bool = False) -> tuple[pathlib.Path, pathlib.Path]:
    """Render the sheet as its score now breaks it; a seated render keeps the chord offsets already written.

    Returns:
        The score path and the rendered probe.

    Raises:
        DoesNotFitError: when a seated render finds no seated score.

    """
    score_path = sheet.path(label)
    if seated and not score_path.exists():
        msg = f"{score_path.name}: no seated score to render; its edits would be lost"
        raise port.DoesNotFitError(msg)
    if not seated:
        port.edited(sheet.imported(), score_path, sheet.size, sheet.distance, sheet.score)
        rewrite(score_path, placed)
    probe = score_path.with_name(score_path.stem + "-layout.pdf")
    BATCHER.render(score_path, [probe, probe.with_suffix(".mpos")], sheet.options)
    return score_path, probe


def candidate(src: pathlib.Path, work: pathlib.Path, variant: str, size: float) -> Candidate:
    """1 size planned and rendered once: whether its lines held, how full its pages are, and its short-line share.

    Returns:
        The candidate's measurements.

    """
    with BATCHER.worker():
        sheet = Sheet(src, work, variant, size)
        widths, room = measure(sheet)
        sheet.set_lines(plan(sheet.score, widths, room, sheet.variant))
        _, probe = render(sheet, "plan")
        wanted = sheet.starts()
        held = sorted({1, *port.system_starts(probe)}) == wanted
        lines = port.rows(sheet.score)
        good = held
        return {
            "size": size,
            "held": held,
            "share": port.short_line_share(lines),
            "fill": port.page_fill(probe),
            "good": good,
            "lines": len(lines),
            "lone": bool(piano.lone_lines(wanted, len(sheet.score.bars))),
        }


def ranked(fit: list[Candidate]) -> list[float]:
    """Every sheet's sizes by fewest lines, then those filling their pages, then largest.

    A dense sheet reads more of the song per glance.

    Returns:
        The sizes, best first.

    """
    return [
        c["size"]
        for c in sorted(
            fit,
            key=lambda c: (c["size"] < port.SIZE_FLOORS[0], c["lines"], c["fill"] < port.PAGE_FILL, -c["size"]),
        )
    ]


def choose(found: list[Candidate], variant: str) -> list[float]:
    """Sizes to finish, best first, within the base short-line share, then the wider one.

    Each tier ranks by fewest lines, then filling their pages, then largest.

    Returns:
        The sizes to finish, best first.

    """
    del variant
    order: list[float] = []
    for share in (port.MAX_SHORT_SHARE, port.RELAXED_SHORT_SHARE):
        fit = [c for c in found if c["good"] and not c["lone"] and c["share"] <= share]
        order += [size for size in ranked(fit) if size not in order]
    return order


def overflowing(sheet: Sheet, probe: pathlib.Path, *, seated: bool) -> set[int]:
    """0-based bars of every line the render wrapped, and once seated, every line still crowded.

    Returns:
        The 0-based bars that overflow.

    """
    wanted, got = sheet.starts(), sorted({1, *port.system_starts(probe)})
    extra = set(got) - set(wanted)
    crowded = port.crowded_rows(probe, wanted) | merged_rows(sheet.score, probe) if seated and not extra else set()
    bad: set[int] = set()
    for row in port.rows(sheet.score):
        if any(row[0] < bar <= row[-1] for bar in extra):
            bad |= {bar - 1 for bar in row}
        elif row[0] in crowded:
            bound = {bar - 1 for bar in row if bar - 1 < len(sheet.shares) and sheet.shares[bar - 1] > 1}
            bad |= bound or {bar - 1 for bar in row}
    return bad


def merged_rows(score: Score, probe: pathlib.Path) -> set[int]:
    """First bar of every line printing fewer chords than the score holds.

    2 chords that touch print as 1 word, the tightest crowding of all.

    Returns:
        The first bar of every merged line.

    """
    rows = [row for row in port.rows(score) if any(score.bars[number - 1].chords for number in row)]
    lines = chordseat.page_lines(probe)
    if len(rows) != len(lines):
        return set()
    return {
        row[0]
        for row, line in zip(rows, lines, strict=True)
        if sum(len(score.bars[number - 1].chords) for number in row) > len(line["chords"])
    }


SIGN_REACH = 1.0
SIGN_REACH_MARGIN = 0.25
INK_LIMIT = 5.0


RESPONSE_FLOOR = 0.2
RESPONSE_CEILING = 5.0
MOVED_ASK_FLOOR = 0.01


def next_bias(
    bias: list[float],
    gaps: list[float | None],
    spaces: list[float],
    before: list[float] | None = None,
    gaps_before: list[float | None] | None = None,
) -> list[float]:
    """Each chord's correction plus its measured gap in staff spaces, scaled by how far its last correction moved its ink.

    A gap past the ink limit is left out: only a mispaired chord is that far off.

    Returns:
        Each chord's next correction.

    """
    found = []
    for at, (gap, space) in enumerate(zip(gaps, spaces, strict=True)):
        was = bias[at] if at < len(bias) else 0.0
        step = (gap or 0.0) / space
        asked = was - before[at] if before is not None and at < len(before) else 0.0
        old = gaps_before[at] if gaps_before is not None and at < len(gaps_before) else None
        if gap is not None and old is not None and abs(asked) > MOVED_ASK_FLOOR:
            response = (old - gap) / space / asked
            if RESPONSE_FLOOR <= response <= RESPONSE_CEILING:
                step /= response
        found.append(was + step if abs(step) <= INK_LIMIT else was)
    return found


@dataclass
class _InkState:
    probe: pathlib.Path
    reach: float
    bias: list[float] = field(default_factory=list)
    inks: list[float] = field(default_factory=list)
    before: list[float] | None = None
    gaps_before: list[float | None] | None = None


def _tracing() -> bool:
    return bool(os.environ.get(TRACE_VARIABLE, ""))


def _round_tags(state: _InkState, noted: list[int] | None) -> dict[str, str]:
    tags = {
        "museSignReach": f"{state.reach:.4f}",
        "museChordGap": f"{CLEAR_SPACES:.4f}",
        "museInkBias": ",".join(f"{b:.4f}" for b in state.bias),
        "museInkWidth": ",".join(f"{w:.4f}" for w in state.inks),
    }
    if noted is not None:
        tags["museNoted"] = ",".join(str(i) for i in noted) or "-1"
        tags["museEven"] = "0"
        tags["museWiden"] = "1" if mode.PRINT else "0"
    return tags


def _ink_round(
    score_path: pathlib.Path,
    rendered: Callable[[], pathlib.Path],
    holds: Callable[[pathlib.Path], bool],
    noted: list[int] | None,
    state: _InkState,
) -> Literal["refused", "settled", "again"]:
    set_tags(score_path, _round_tags(state, noted))
    run_extension(score_path, score_path)
    state.probe = rendered()
    if not holds(state.probe):
        return "refused"
    _, text = chordseat.score_text(score_path)
    rows = chordseat.rows_of(text)
    lines = chordseat.matched(text, state.probe)
    if [len(line["chords"]) for line in lines] != [len(row) for row in rows if row]:
        return "refused"
    gaps = chordseat.gaps(rows, lines)
    spaces = [chordseat.space_points(score_path)] * len(gaps)
    if _tracing():
        off = [
            (at + 1, round(gap, 2))
            for at, gap in enumerate(gaps)
            if gap is not None and abs(gap - chordseat.GAP) > chordseat.TOLERANCE
        ]
        sys.stdout.write(f"{score_path.name}: seating round, chords off target {off}" + "\n")
        sys.stdout.flush()
    state.bias, state.before, state.gaps_before = (
        next_bias(state.bias, gaps, spaces, state.before, state.gaps_before),
        state.bias,
        gaps,
    )
    state.inks = [width / space for width, space in zip(chordseat.ink_widths(rows, lines), spaces, strict=True)]
    evened = noted is not None and chordseat.equalize(score_path, state.probe, {index + 1 for index in noted})
    overlaps = pdfcheck.sign_word_overlaps(state.probe) + [
        width for _, width in chordseat.jump_crossings(score_path, state.probe)
    ]
    if overlaps:
        state.reach += max(overlaps) / spaces[0] + SIGN_REACH_MARGIN
    if (
        not evened
        and not overlaps
        and all(gap is None or abs(gap - chordseat.GAP) <= chordseat.TOLERANCE for gap in gaps)
        and not pdfcheck.chord_collisions(state.probe)
    ):
        return "settled"
    return "again"


def _settle(
    score_path: pathlib.Path,
    rendered: Callable[[], pathlib.Path],
    noted: list[int] | None,
    state: _InkState,
) -> None:
    for round_number in range(INK_ROUNDS):
        state.probe = current_render(score_path, state.probe, rendered)
        _, text = chordseat.score_text(score_path)
        rows = chordseat.rows_of(text)
        lines = chordseat.matched(text, state.probe)
        if [len(line["chords"]) for line in lines] != [len(row) for row in rows if row]:
            break
        gaps = chordseat.gaps(rows, lines)
        off = [
            (at + 1, round(gap, 2))
            for at, gap in enumerate(gaps)
            if gap is not None and abs(gap - chordseat.GAP) > chordseat.TOLERANCE
        ]
        noted_bars = {index + 1 for index in noted} if noted is not None else set()
        uneven = chordseat.uneven(score_path, state.probe, noted_bars) if noted is not None else []
        if _tracing():
            sys.stdout.write(f"{score_path.name}: settling, chords off target {off}, uneven {uneven}" + "\n")
            sys.stdout.flush()
        if (not off and not uneven) or round_number == INK_ROUNDS - 1:
            break
        if uneven:
            chordseat.equalize(score_path, state.probe, noted_bars)
        spaces = [chordseat.space_points(score_path)] * len(gaps)
        state.bias, state.before, state.gaps_before = (
            next_bias(state.bias, gaps, spaces, state.before, state.gaps_before),
            state.bias,
            gaps,
        )
        set_tags(
            score_path,
            {
                "museSignReach": f"{state.reach:.4f}",
                "museInkBias": ",".join(f"{b:.4f}" for b in state.bias),
                "museInkWidth": ",".join(f"{w:.4f}" for w in state.inks),
                "museEven": "0",
            },
        )
        run_extension(score_path, score_path)
        state.probe = rendered()


def seat_inked(
    score_path: pathlib.Path,
    rendered: Callable[[], pathlib.Path],
    holds: Callable[[pathlib.Path], bool],
    noted: list[int] | None = None,
) -> pathlib.Path:
    """Fit each bar's chords through the extension, even empty bars and seat chords.

    Each round passes it every chord's measured ink offset and ink width, until the ink sits on target.

    Returns:
        The render of the seated score.

    """
    state = _InkState(probe=score_path, reach=SIGN_REACH)
    for _ in range(INK_ROUNDS):
        outcome = _ink_round(score_path, rendered, holds, noted, state)
        if outcome == "refused":
            return state.probe
        if outcome == "settled":
            break
    _settle(score_path, rendered, noted, state)
    return current_render(score_path, state.probe, rendered)


def current_render(score_path: pathlib.Path, probe: pathlib.Path, rendered: Callable[[], pathlib.Path]) -> pathlib.Path:
    """Return the render of the score file as it now stands.

    A file written after its render is rendered again, so nothing judges a print that is not its own.

    Returns:
        The current render.

    """
    return rendered() if score_path.stat().st_mtime_ns > probe.stat().st_mtime_ns else probe


def seat(sheet: Sheet, score_path: pathlib.Path) -> pathlib.Path:
    """Seat a sheet's final score in place, every round judged on a fresh render of the saved score.

    Returns:
        The render of the seated score.

    """
    noted = [index for index, bar in enumerate(sheet.score.bars) if piano.has_notes(bar)]
    return seat_inked(
        score_path,
        lambda: render(sheet, "final", seated=True)[1],
        lambda probe: sheet.starts() == sorted({1, *port.system_starts(probe)}),
        noted,
    )


def finish(
    src: pathlib.Path,
    work: pathlib.Path,
    variant: str,
    size: float,
) -> tuple[Score, pathlib.Path, float, float, int]:
    """1 size finished at the base line gap, then at wider gaps while text below 1 line meets the next line's chords.

    Returns:
        The finished score, its path, size, line gap and page count.

    Raises:
        DoesNotFitError: when no line gap passes.

    """
    refusal: port.DoesNotFitError | None = None
    learned: list[list[float]] = []
    for step in range(int(GAP_REACH / GAP_STEP) + 1):
        try:
            return finish_at(src, work, variant, port.Sizing(size, port.BASE_DISTANCE + step * GAP_STEP), learned)
        except port.DoesNotFitError as error:
            if "text on text" not in str(error):
                raise
            refusal = error
            sys.stdout.write(
                f"gap step {src.stem} {variant} {size} {port.BASE_DISTANCE + step * GAP_STEP}: {error}" + "\n",
            )
            sys.stdout.flush()
    if refusal is None:
        msg = f"{src.stem} at {size}: no line gap was tried"
        raise port.DoesNotFitError(msg)
    raise refusal


def uneven_empty_bars(sheet: Sheet, score_path: pathlib.Path, probe: pathlib.Path) -> list[str]:
    """Apply the gate's even-bars rule in strict mode: the empty bars of 1 line are equal.

    Print mode lets a bar widen for its own chords, which the gate alone measures.

    Returns:
        The uneven-bar problems.

    """
    if mode.PRINT:
        return []
    return chordseat.uneven(score_path, probe, {bar.number for bar in sheet.score.bars if piano.has_notes(bar)})


def _fitted(sheet: Sheet, score_path: pathlib.Path) -> tuple[set[int], pathlib.Path]:
    _, probe = render(sheet, "final")
    bad = overflowing(sheet, probe, seated=False)
    if not bad:
        if port.balance(sheet.score, probe):
            _, probe = render(sheet, "final")
            bad = overflowing(sheet, probe, seated=False)
        if not bad:
            probe = seat(sheet, score_path)
            bad = overflowing(sheet, probe, seated=True)
        for _ in range(BALANCE_ROUNDS):
            if bad or not port.page_problems(sheet.score, probe) or not port.balance(sheet.score, probe):
                break
            render(sheet, "final")
            probe = seat(sheet, score_path)
            bad = overflowing(sheet, probe, seated=True)
    return bad, probe


def _grown(
    sheet: Sheet,
    widths: list[float],
    room: float,
    bad: set[int],
    tried: set[frozenset[int]],
) -> list[float] | None:
    grown = widths
    for _ in range(GROW_STEPS):
        grown = [width * GROW if index in bad else width for index, width in enumerate(grown)]
        if frozenset(plan(sheet.score, grown, room, sheet.variant)) not in tried:
            return grown
    return None


def _final_problems(sheet: Sheet, score_path: pathlib.Path, probe: pathlib.Path) -> list[str]:
    problems = (
        pdfcheck.all_problems(probe, sheet.title)
        + chordseat.problems(score_path, probe)
        + chordseat.crowded_line_texts(score_path, probe)
        + port.line_rule_reasons(sheet.score, sheet.variant)
        + uneven_empty_bars(sheet, score_path, probe)
        + port.page_problems(sheet.score, probe)
    )
    problems += port.short_line_problems(port.rows(sheet.score))
    problems += port.chord_starts(score_path, sheet.score)
    return problems


def progress(best: int, stalled: int, count: int) -> tuple[int, int]:
    """Track a replanning search by its fewest overflowing bars, and how many plans since it last fell.

    Returns:
        The fewest overflowing bars so far and the plans made since that number last fell.

    """
    return (count, 0) if count < best else (best, stalled + 1)


def finish_at(
    src: pathlib.Path,
    work: pathlib.Path,
    variant: str,
    sizing: port.Sizing,
    learned: list[list[float]] | None = None,
) -> tuple[Score, pathlib.Path, float, float, int]:
    """1 size and line gap from plan to a seated sheet that passes every final check.

    A line that will not hold grows its estimate and the whole plan is made again.

    Returns:
        The finished score, its path, size, line gap and page count.

    Raises:
        DoesNotFitError: when lines still overflow or a final check fails.

    """
    size, distance = sizing
    sheet = Sheet(src, work, variant, size, distance)
    widths, room = measure(sheet)
    if learned is not None and len(learned) > 0:
        widths = learned[-1]
    score_path = sheet.path("final")
    tried: set[frozenset[int]] = set()
    best, stalled = sys.maxsize, 0
    while True:
        sheet.set_lines(plan(sheet.score, widths, room, sheet.variant))
        tried.add(frozenset(sheet.starts()))
        bad, probe = _fitted(sheet, score_path)
        if _tracing():
            sys.stdout.write(
                f"{sheet.name} {size}: starts {sheet.starts()} overflowing {sorted(b + 1 for b in bad)}" + "\n",
            )
            sys.stdout.flush()
        if not bad:
            break
        best, stalled = progress(best, stalled, len(bad))
        if stalled >= STALL_ROUNDS:
            msg = (
                f"{sheet.name} at {size}: lines still overflow, {best} bars at best, with no fewer in {STALL_ROUNDS} plans"
            )
            raise port.DoesNotFitError(msg)
        grown = _grown(sheet, widths, room, bad, tried)
        if grown is None:
            msg = (
                f"{sheet.name} at {size}: bars {sorted(b + 1 for b in bad)} overflow and no plan can move them: "
                f"{port.crowded_reasons(probe)[:3]}"
            )
            raise port.DoesNotFitError(
                msg,
            )
        widths = grown
        if learned is not None:
            learned.append(widths)
    probe = current_render(score_path, probe, lambda: render(sheet, "final", seated=True)[1])
    problems = _final_problems(sheet, score_path, probe)
    if problems:
        msg = f"{sheet.name} at {size}: {problems[:3]}"
        raise port.DoesNotFitError(msg)
    return sheet.score, score_path, size, sheet.distance, len(pdftext.read(probe))


FINISHED_AT_ONCE = 3


def best_finished(
    order: list[float],
    planned: dict[float, int],
    finished: Callable[[float], tuple[Score, pathlib.Path, float, float, int]],
) -> tuple[tuple[Score, pathlib.Path, float, float, int] | None, list[str]]:
    """Finish sizes several at a time in their order, and keep the one with the fewest finished lines.

    A size not yet started is skipped once its planned lines cannot beat the best finished, since finishing only adds
    lines; ties go to the size earlier in the order, so the choice is the one finishing them 1 by 1 would make.

    Returns:
        The best finished sheet, or none, and every refusal.

    """
    done: dict[float, tuple[Score, pathlib.Path, float, float, int]] = {}
    refusals: list[str] = []

    def fewest() -> int | None:
        return min((len(port.rows(sheet[0])) for sheet in done.values()), default=None)

    waiting = list(order)
    running: dict[concurrent.futures.Future[tuple[Score, pathlib.Path, float, float, int]], float] = {}
    began: dict[float, float] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=FINISHED_AT_ONCE) as pool:
        while waiting or running:
            while waiting and len(running) < FINISHED_AT_ONCE:
                size = waiting.pop(0)
                best = fewest()
                if best is not None and planned.get(size, 0) >= best:
                    continue
                running[pool.submit(finished, size)] = size
                began[size] = time.monotonic()
            if not running:
                break
            ready, _ = concurrent.futures.wait(running, return_when=concurrent.futures.FIRST_COMPLETED)
            for future in ready:
                size = running.pop(future)
                if isinstance(error := future.exception(), port.DoesNotFitError):
                    refusals.append(str(error))
                    sys.stdout.write(
                        f"refused after {time.monotonic() - began[size]:.0f} s, planned {planned.get(size)} lines: {error}"
                        "\n",
                    )
                    sys.stdout.flush()
                    continue
                done[size] = future.result()
                sys.stdout.write(
                    f"finished {size} after {time.monotonic() - began[size]:.0f} s: "
                    f"planned {planned.get(size)} lines, finished {len(port.rows(done[size][0]))}" + "\n",
                )
                sys.stdout.flush()
    if not done:
        return None, refusals
    rank = {size: at for at, size in enumerate(order)}
    winner = min(done, key=lambda size: (len(port.rows(done[size][0])), rank[size]))
    return done[winner], refusals


def lay(src: pathlib.Path, work: pathlib.Path, variant: str) -> tuple[Score, pathlib.Path, float, float, int]:
    """Every candidate size planned side by side in shared jobs; the best is finished, the next only if it cannot be.

    Returns:
        The best finished score, its path, size, line gap and page count.

    Raises:
        DoesNotFitError: when no size passes.

    """
    sizes = port.sizes_between(port.SIZE_FLOORS[-1], port.SIZE_CAP)
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(sizes)) as pool:
        found = list(pool.map(lambda size: candidate(src, work, variant, size), sizes))
    planned = {c["size"]: c["lines"] for c in found}
    best, refusals = best_finished(choose(found, variant), planned, lambda size: finish(src, work, variant, size))
    if best is not None:
        return best
    summary = [
        (c["size"], "held" if c["held"] else "wrapped", f"short {c['share']:.0%}", "lone bar" if c["lone"] else "")
        for c in found
    ]
    msg = f"{src.stem} {variant}: no size passes: {refusals[:3] or summary}"
    raise port.DoesNotFitError(msg)
