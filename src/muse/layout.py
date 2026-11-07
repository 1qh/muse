"""Calculated layout: measure a sheet's natural bar widths once, plan its lines in code, render the plan, and correct only what measurement shows off."""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import pathlib
import re
import shutil
import threading
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from muse import chordseat, launch, mode, pdfcheck, pdftext, piano, port
from muse.model import Score
from muse.render import BATCHER
from muse.song import decode, song_of

LINE_SHARE = 0.93  # of a line's width a plan fills; the rest is the clef and key each line carries
WIDE_PAGE = 200.0  # inches: wide enough that a whole song prints on 1 line at its natural width
REPLAN_ROUNDS = 16
IMPORT_SIZE = 1.5  # the staff space the shared import is made at; each size sets its own on an edited copy
OVERFLOW_BREAKS = 1000  # a line too wide cannot print at all, so it outweighs every soft line rule
SHORT_COST = 0.15  # a 2-bar line is allowed, but a fuller arrangement of as many lines wins
GROW = 1.06  # share a bar's width estimate grows each time its line will not hold
GAP_STEPS = 4  # line gaps tried before a size is refused for text meeting text
GAP_STEP = 2.0  # staff spaces the line gap widens each try
GROW_STEPS = 40  # up to about 10 times, so a bar whose own chords need a whole line gets one before it is called unmovable
INK_ROUNDS = 6
EXTENSION = pathlib.Path(__file__).parent / "extension"
EXTENSION_URI = "extension://muse/seat"
EXTENSION_FOLDER = "museseat"
INSTALL_LOCK = threading.Lock()
EXTENSION_SECONDS = 300


def extensions_root() -> pathlib.Path:
    """Where MuseScore reads user extensions; MUSE_EXTENSIONS overrides it."""
    given = os.environ.get("MUSE_EXTENSIONS")
    return pathlib.Path(given) if given else pathlib.Path.home() / "Library" / "Application Support" / "MuseScore" / "MuseScore4" / "extensions"


def install_extension() -> str:
    """Install the pipeline's own extension under a name carrying a digest of its bytes, and return the address to run it by: 2 versions never share 1 copy."""
    script = (EXTENSION / "main.js").read_bytes()
    digest = hashlib.sha256(script + (EXTENSION / "manifest.json").read_bytes()).hexdigest()[:12]
    uri = f"{EXTENSION_URI}-{digest}"
    home = extensions_root() / f"{EXTENSION_FOLDER}-{digest}"
    with INSTALL_LOCK:  # the threads of 1 process install 1 at a time; other processes race only through the atomic rename
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
        except OSError:  # another process installed the same bytes first
            shutil.rmtree(fresh, ignore_errors=True)
        shutil.rmtree(extensions_root() / EXTENSION_FOLDER, ignore_errors=True)  # the copy at a fixed address that every version once shared
    return uri


def run_extension(source: pathlib.Path, target: pathlib.Path) -> None:
    """1 MuseScore launch: open the score, seat its chords by measuring its own layout, save it."""
    uri = install_extension()
    done = launch.mscore(["--extension", uri, "-o", str(target), str(source)], EXTENSION_SECONDS)
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
    import_style: pathlib.Path = field(init=False)
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
        self.import_style = port.styled(self.work, f"{self.name}-import.mss", info.title, IMPORT_SIZE, self.distance)  # every size edits 1 shared import

    def path(self, label: str) -> pathlib.Path:
        return self.work / f"{self.name}-{self.size:g}-{label}.mscz"

    def imported(self) -> pathlib.Path:
        return port.imported(self.score, self.work, self.name, self.import_style, False)

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
    shares = chord_shares(natural, len(laid_natural), [index for index, bar in enumerate(sheet.score.bars) if piano.has_notes(bar)])
    widths = [box["sx"] * share for box, share in zip(laid_natural, shares, strict=True)]
    laid = boxes(normal.with_suffix(".mpos"))
    first = [box for box in laid if box["page"] == laid[0]["page"] and box["y"] == laid[0]["y"]]
    return widths, sum(box["sx"] for box in first)


def chord_shares(natural: pathlib.Path, bars: int, noted: list[int]) -> list[float]:
    """Per bar, the share its natural width must grow to hold its chords apart by the clear gap, measured by the extension."""
    probe = natural.with_name(natural.stem + "-chords.mscz")
    shutil.copyfile(natural, probe)
    set_tags(probe, {"museMode": "measure", "museChordGap": f"{CLEAR_SPACES:.4f}", "museNoted": ",".join(str(i) for i in noted) or "-1"})
    run_extension(probe, probe)
    crowded = read_tag(probe, "museSeatError")
    if crowded:
        raise port.DoesNotFit(f"{natural.name}: measuring chord room: {crowded}")
    shares = [float(value) for value in read_tag(probe, "museChordShare").split(",")]
    if len(shares) != bars:
        raise port.DoesNotFit(f"{natural.name}: the extension measured {len(shares)} bars of {bars}")
    return shares


def blocks_of(score: Score, variant: str | None) -> list[list[int]]:
    """0-based bars of each section; a section of 1 bar joins the block after it, so that block's mark or label sits second rather than the bar standing alone."""
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
    """1-based line starts: per section, the break set with fewest rule breaks, then fewest lines, then the most even fill."""
    cap = room * LINE_SHARE
    tied = [port.tied_out(bar) for bar in score.bars]
    empty = [not piano.has_notes(bar) for bar in score.bars]
    chorded = [bool(bar.chords) for bar in score.bars]

    def width(line: list[int]) -> float:
        """A line's width once its empty bars are evened to the widest of them, as they will be."""
        widest = max((widths[i] for i in line if empty[i]), default=0.0)
        return sum(widest if empty[i] else widths[i] for i in line)

    def judged(line: list[int], opens_block: bool, ends_block: bool, lone_allowed: bool) -> tuple[int, float]:
        used = width(line)
        lone = len(line) == 1 and not lone_allowed  # a lone bar breaks a rule the gate holds, however wide its estimate has grown
        breaks = OVERFLOW_BREAKS * (used > cap and len(line) > 1) + lone + (not opens_block and not chorded[line[0]]) + (not ends_block and tied[line[-1]])
        cost = ((cap - used) / cap) ** 2 if used <= cap else 0.0
        if len(line) == 2:
            cost += SHORT_COST
        return breaks, cost

    starts: set[int] = set()
    for block in blocks_of(score, variant):
        count = len(block)
        best: list[tuple[int, int, float, int] | None] = [None] * (count + 1)  # rule breaks, lines, cost, where the last line starts
        best[0] = (0, 0, 0.0, -1)
        for end in range(1, count + 1):
            for begin in range(end):
                before = best[begin]
                if before is None:
                    continue
                breaks, cost = judged(block[begin:end], begin == 0, end == count, count == 1)
                option = (before[0] + breaks, before[1] + 1, before[2] + cost, begin)
                if best[end] is None or option[:3] < best[end][:3]:  # type: ignore[index]
                    best[end] = option
        at = count
        while at > 0:
            chosen = best[at]
            assert chosen is not None
            starts.add(block[chosen[3]])
            at = chosen[3]
    return {index + 1 for index in starts}


def render(sheet: Sheet, label: str, seated: bool = False) -> tuple[pathlib.Path, pathlib.Path]:
    """The sheet as its score now breaks it; a seated render keeps the chord offsets already written."""
    score_path = sheet.path(label)
    if seated and not score_path.exists():
        raise port.DoesNotFit(f"{score_path.name}: no seated score to render; its edits would be lost")
    if not seated:
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
        sheet.set_lines(plan(sheet.score, widths, room, sheet.variant))
        _, probe = render(sheet, "plan")
        wanted = sheet.starts()
        held = sorted({1, *port.system_starts(probe)}) == wanted
        lines = port.rows(sheet.score)
        good = held  # chords are seated, and so judged for crowding, only on the size that is finished
        return {"size": size, "held": held, "share": port.short_line_share(lines), "fill": port.page_fill(probe), "good": good, "lines": len(lines),
                "lone": bool(piano.lone_lines(wanted, len(sheet.score.bars)))}  # no stranded single bar on any sheet


def ranked(fit: list[dict[str, Any]], variant: str) -> list[float]:
    """A piano sheet's sizes by fewest lines, then largest; a band sheet's largest first, those filling their pages ahead."""
    if variant == port.PIANO:
        return [c["size"] for c in sorted(fit, key=lambda c: (c["lines"], -c["size"]))]
    return sorted((c["size"] for c in fit if c["fill"] >= port.PAGE_FILL), reverse=True) + sorted((c["size"] for c in fit if c["fill"] < port.PAGE_FILL), reverse=True)


def choose(found: list[dict[str, Any]], variant: str) -> list[float]:
    """Sizes to finish, best first, within the base short-line share, then the wider one: a band sheet's largest whose lines fill their pages; a piano sheet's fewest lines, then its largest of those, so a pianist sees the most chords at once."""
    order: list[float] = []
    for share in (port.MAX_SHORT_SHARE, port.RELAXED_SHORT_SHARE):
        fit = [c for c in found if c["good"] and not c["lone"] and c["share"] <= share]
        order += [size for size in ranked(fit, variant) if size not in order]
    return order


def overflowing(sheet: Sheet, probe: pathlib.Path, seated: bool) -> set[int]:
    """0-based bars of every line the render wrapped, and once seated, every line still crowded."""
    wanted, got = sheet.starts(), sorted({1, *port.system_starts(probe)})
    extra = set(got) - set(wanted)
    crowded = port.crowded_rows(probe, wanted) | merged_rows(sheet.score, probe) if seated and not extra else set()
    return {bar - 1 for row in port.rows(sheet.score) if row[0] in crowded or any(row[0] < bar <= row[-1] for bar in extra) for bar in row}


def merged_rows(score: Score, probe: pathlib.Path) -> set[int]:
    """First bar of every line printing fewer chords than the score holds: 2 chords that touch print as 1 word, the tightest crowding of all."""
    rows = [row for row in port.rows(score) if any(score.bars[number - 1].chords for number in row)]
    lines = chordseat.page_lines(probe)
    if len(rows) != len(lines):
        return set()  # lines that do not pair are the wrapped-line check's to report
    return {row[0] for row, line in zip(rows, lines, strict=True) if sum(len(score.bars[number - 1].chords) for number in row) > len(line["chords"])}


INK_LIMIT = 5.0  # staff spaces a measured offset may correct: a line's opening read 2 ways differs by up to about 3, a mispaired chord sits a chord's distance away


def next_bias(bias: list[float], gaps: list[float | None], spaces: list[float]) -> list[float]:
    """Each chord's correction plus its measured gap in staff spaces, a gap past the ink limit left out: only a mispaired chord is that far off."""
    found = []
    for at, (gap, space) in enumerate(zip(gaps, spaces, strict=True)):
        was = bias[at] if at < len(bias) else 0.0
        step = (gap or 0.0) / space
        found.append(was + step if abs(step) <= INK_LIMIT else was)
    return found


def seat_inked(score_path: pathlib.Path, rendered: Callable[[], pathlib.Path], holds: Callable[[pathlib.Path], bool], noted: list[int] | None = None) -> pathlib.Path:
    """The extension fits each bar's chords, evens empty bars and seats chords; each round passes it every chord's measured ink offset and ink width, until the ink sits on target."""
    bias: list[float] = []
    inks: list[float] = []  # each chord's ink width in staff spaces, known after the first round
    probe = score_path
    evened = False
    for _ in range(INK_ROUNDS):
        tags = {"museChordGap": f"{CLEAR_SPACES:.4f}", "museInkBias": ",".join(f"{b:.4f}" for b in bias), "museInkWidth": ",".join(f"{w:.4f}" for w in inks)}
        if noted is not None:  # the caller evens empty bars itself, from each fresh render, so the extension leaves them
            tags["museNoted"] = ",".join(str(i) for i in noted) or "-1"
            tags["museEven"] = "0"
            tags["museWiden"] = "1" if mode.PRINT else "0"  # print mode lets a bar widen for its own crowded chords
        set_tags(score_path, tags)
        run_extension(score_path, score_path)
        probe = rendered()  # a fresh render of the saved score: the layout inside the launch is not what prints
        if not holds(probe):
            return probe  # widening wrapped a line; the caller plans again
        _, text = chordseat.score_text(score_path)
        rows = chordseat.rows_of(text)
        lines = chordseat.matched(text, probe)
        if [len(line["chords"]) for line in lines] != [len(row) for row in rows if row]:
            return probe  # chords ran together into 1 printed name: the line is crowded, and the caller plans it again with fewer bars
        gaps = chordseat.gaps(rows, lines)
        spaces = [chordseat.space_points(score_path)] * len(gaps)
        bias = next_bias(bias, gaps, spaces)
        inks = [width / space for width, space in zip(chordseat.ink_widths(rows, lines), spaces, strict=True)]
        evened = noted is not None and chordseat.equalize(score_path, probe, {index + 1 for index in noted})  # measured on the print, both ways
        if not evened and all(gap is None or abs(gap - chordseat.GAP) <= chordseat.TOLERANCE for gap in gaps) and not pdfcheck.chord_collisions(probe):
            break
    if evened:  # the last round evened bars after its render, so the chords must be seated against the new widths once more, with its latest measurements
        set_tags(score_path, {"museInkBias": ",".join(f"{b:.4f}" for b in bias), "museInkWidth": ",".join(f"{w:.4f}" for w in inks)})
        run_extension(score_path, score_path)
        probe = rendered()
    return probe


def seat(sheet: Sheet, score_path: pathlib.Path) -> pathlib.Path:
    """A sheet's final score seated in place, every round judged on a fresh render of the saved score."""
    noted = [index for index, bar in enumerate(sheet.score.bars) if piano.has_notes(bar)]
    return seat_inked(score_path, lambda: render(sheet, "final", seated=True)[1], lambda probe: sheet.starts() == sorted({1, *port.system_starts(probe)}), noted)


def finish(src: pathlib.Path, work: pathlib.Path, variant: str, size: float) -> tuple[Score, pathlib.Path, float, float, int]:
    """1 size finished at the base line gap, then at wider gaps while text below 1 line meets the next line's chords."""
    refusal: port.DoesNotFit | None = None
    learned: list[list[float]] = []  # widths a gap's replans settled on: a wider gap moves lines apart, never bars across them
    for step in range(GAP_STEPS):
        try:
            return finish_at(src, work, variant, size, port.BASE_DISTANCE + step * GAP_STEP, learned)
        except port.DoesNotFit as error:
            if "text on text" not in str(error):
                raise
            refusal = error
    assert refusal is not None
    raise refusal


def finish_at(src: pathlib.Path, work: pathlib.Path, variant: str, size: float, distance: float, learned: list[list[float]] | None = None) -> tuple[Score, pathlib.Path, float, float, int]:
    """1 size and line gap from plan to a seated sheet that passes every final check; a line that will not hold grows its estimate and the whole plan is made again."""
    sheet = Sheet(src, work, variant, size, distance)
    widths, room = measure(sheet)
    if learned:
        widths = learned[-1]
    score_path = sheet.path("final")
    tried: set[frozenset[int]] = set()
    for _ in range(REPLAN_ROUNDS):
        sheet.set_lines(plan(sheet.score, widths, room, sheet.variant))
        tried.add(frozenset(sheet.starts()))
        _, probe = render(sheet, "final")
        bad = overflowing(sheet, probe, seated=False)
        if not bad:
            if port.balance(sheet.score, probe):
                _, probe = render(sheet, "final")
                bad = overflowing(sheet, probe, seated=False)
            if not bad:
                probe = seat(sheet, score_path)
                bad = overflowing(sheet, probe, seated=True)
        if os.environ.get("MUSE_LAYOUT_TRACE"):
            print(f"{sheet.name} {size}: starts {sheet.starts()} overflowing {sorted(b + 1 for b in bad)}", flush=True)
        if not bad:
            break
        grown = widths
        for _ in range(GROW_STEPS):  # widen those bars until the plan actually changes; 1 step is often too little to cross a break
            grown = [width * GROW if index in bad else width for index, width in enumerate(grown)]
            if frozenset(plan(sheet.score, grown, room, sheet.variant)) not in tried:  # a plan already rendered would only repeat its fault
                break
        else:
            raise port.DoesNotFit(f"{sheet.name} at {size}: bars {sorted(b + 1 for b in bad)} overflow and no plan can move them: {port.crowded_reasons(probe)[:3]}")
        widths = grown
        if learned is not None:
            learned.append(widths)
    else:
        raise port.DoesNotFit(f"{sheet.name} at {size}: lines still overflow after {REPLAN_ROUNDS} plans")
    problems = pdfcheck.all_problems(probe, sheet.title) + chordseat.problems(score_path, probe) + chordseat.crowded_line_texts(score_path, probe) + port.line_rule_breaks(sheet.score, sheet.variant)
    if problems:
        raise port.DoesNotFit(f"{sheet.name} at {size}: {problems[:3]}")
    return sheet.score, score_path, size, sheet.distance, len(pdftext.open(probe))


def lay(src: pathlib.Path, work: pathlib.Path, variant: str) -> tuple[Score, pathlib.Path, float, float, int]:
    """Every candidate size planned side by side in shared jobs; the best is finished, the next only if it cannot be."""
    sizes = port.sizes_between(port.SIZE_FLOORS[-1], port.SIZE_CAP)
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(sizes)) as pool:
        found = list(pool.map(lambda size: candidate(src, work, variant, size), sizes))
    refusals = []
    for size in choose(found, variant):
        try:
            return finish(src, work, variant, size)
        except port.DoesNotFit as error:
            refusals.append(str(error))
            print(f"refused {error}", flush=True)  # each refused size and its reason, as it happens
    summary = [(c["size"], "held" if c["held"] else "wrapped", f"short {c['share']:.0%}", "lone bar" if c["lone"] else "") for c in found]
    raise port.DoesNotFit(f"{src.stem} {variant}: no size passes: {refusals[:3] or summary}")

