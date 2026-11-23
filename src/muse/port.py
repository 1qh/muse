"""Port 1 source PDF: decode, write MusicXML, create the MuseScore file, transpose, export."""

from __future__ import annotations

import hashlib
import itertools
import operator
import os
import pathlib
import re
import shutil
import threading
import zipfile
from typing import TYPE_CHECKING, Any

from muse import buildcache, chordnames, chordseat, jumps, launch, mode, musicxml, pdfcheck, pdftext, piano

if TYPE_CHECKING:
    from muse.model import Bar, Score
    from muse.song import Song

CHORD_GAP = chordseat.CHORD_GAP
CHORD_SIZE_PER_SPACE = 8.0
SIZE_FLOORS = (1.2, 1.15)
SIZE_CAP = 2.0
PAGE_SLACK = 0.05
PAGE_FILL = mode.pick(0.85, 0.0)
MAX_WORKERS = 10
SIZE_STEP = 0.05
SIGN_CLEARANCE = 3.0
TARGET_HOLD = '<autoplace>0</autoplace><offset x="1" y="-3"/>'
SIGN_STEP = -6
SIGN_RISE = -2.5
SIGN_BAR_STRETCH = 2.5
MAX_SHORT_SHARE = mode.pick(0.15, 0.40)
RELAXED_SHORT_SHARE = mode.pick(0.30, 0.40)
BASE_DISTANCE = 11.0
ORIGINAL = "original"
TONE = "tone"
PIANO = "piano"
STYLE = buildcache.STYLE
SF_PRO_FILES = tuple(f"SF-Pro-Display-{face}.otf" for face in ("Regular", "RegularItalic", "Bold", "BoldItalic"))
FONT_DIRS = (pathlib.Path.home() / "Library" / "Fonts", pathlib.Path("/Library/Fonts"))
NUMBER_MARGIN = 60
SHORT_LINE_BARS = 2
MIN_BALANCED_PAGES = 2
TIE_LOOKBACK = 2


class MissingFontError(RuntimeError):
    """MuseScore would quietly fall back to another font."""


def require_font(folders: tuple[pathlib.Path, ...] = FONT_DIRS) -> None:
    missing = [name for name in SF_PRO_FILES if not any((folder / name).exists() for folder in folders)]
    if missing:
        msg = f"SF Pro Display is missing {missing}; install it from Apple into {folders[0]}"
        raise MissingFontError(msg)


def run(*args: str) -> None:
    done = launch.mscore(list(args), launch.LAUNCH_SECONDS)
    if done.returncode:
        msg = f"mscore {args}: {done.stderr[-400:]}"
        raise RuntimeError(msg)


BOX = (
    "<frameType>1</frameType><frameWidth>0.45</frameWidth><framePadding>0.4</framePadding>"
    '<frameFgColor r="{}" g="{}" b="{}" a="255"/>'
)
SIGN_BOX = {"segno": BOX.format(220, 0, 0), "coda": BOX.format(0, 0, 220)}


def real_jumps(mscz: pathlib.Path, *, clear_chord: bool = False) -> None:
    """Import keeps jump words as staff text; replace them with MuseScore's own jump elements."""
    items = {}
    with zipfile.ZipFile(mscz) as z:
        for info in z.infolist():
            items[info.filename] = z.read(info.filename)
    for name, data in items.items():
        if not name.endswith(".mscx"):
            continue
        text = jumps.rewrite(data.decode("utf-8"), jumps.WORDS, jumps.sign_marker, SIGN_CLEARANCE if clear_chord else None)
        items[name] = text.encode("utf-8")
    with zipfile.ZipFile(mscz, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in items.items():
            z.writestr(name, data)


def strip_title_frame(mscz: pathlib.Path) -> None:
    """Import adds an empty frame for a title the sheet does not carry; it only pushes the music down."""
    with zipfile.ZipFile(mscz) as z:
        items = {info.filename: z.read(info.filename) for info in z.infolist()}
    for name, data in items.items():
        if name.endswith(".mscx"):
            items[name] = re.sub(
                r"\s*<VBox>(?:(?!</VBox>|<Text>).)*</VBox>",
                "",
                data.decode("utf-8"),
                flags=re.DOTALL,
            ).encode(
                "utf-8",
            )
    with zipfile.ZipFile(mscz, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in items.items():
            z.writestr(name, data)


def system_starts(pdf: pathlib.Path) -> list[int]:
    """Read the bar number printed at the start of every line from the exported PDF.

    Returns:
        The bar number printed at the start of every line, read from the exported PDF.

    """
    found = []
    for page in pdftext.open(pdf):
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                found.extend(
                    int(span["text"])
                    for span in line["spans"]
                    if span["text"].strip().isdigit() and span["bbox"][0] < NUMBER_MARGIN and "Italic" in span["font"]
                )
    return found


def rows(score: Score) -> list[list[int]]:
    """1-based bar numbers of each declared row.

    Returns:
        The 1-based bar numbers of each declared row.

    """
    out: list[list[int]] = []
    for number, bar in enumerate(score.bars, 1):
        if not out or bar.new_system or bar.new_page:
            out.append([])
        out[-1].append(number)
    return out


def tied_out(bar: Bar) -> bool:
    """Whether a tie leaves the bar: only a voice's last event can carry one past the barline.

    Returns:
        Whether a tie leaves the bar.

    """
    return any(head.tie_start for voice in [*bar.voices, *bar.voices2] if voice for head in voice[-1].heads)


def last_before(where: list[tuple[int, float, int]], page_no: int) -> int:
    """Find the line a page with no printed bar number continues: the last one numbered on an earlier page.

    Returns:
        The line a page with no printed bar number continues.

    """
    return max(item for item in where if item[0] < page_no)[2]


def crowded_reasons(pdf: pathlib.Path) -> list[str]:
    """Say what makes a line overflow, in words, for a refusal a reader can act on.

    Returns:
        What makes a line overflow, in words.

    """
    return (
        [f"chords too close at {root}" for _, _, root in pdfcheck.chord_collisions(pdf)]
        + [what for _, _, what in pdfcheck.sign_chord_hits(pdf)]
        + [f"past the margin: {word}" for _, _, word in pdfcheck.past_margin(pdf)]
    )


def crowded_rows(pdf: pathlib.Path, firsts: list[int]) -> set[int]:
    """Find the first bar of every line a wider line can cure, matched by page and height.

    Chords too close and ink past the margin; text on text is never a width fault.

    Returns:
        The first bar of every line a wider line can cure.

    """
    where = []
    for page_no, page in enumerate(pdftext.open(pdf)):
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                where.extend(
                    (page_no, span["bbox"][1], int(span["text"]))
                    for span in line["spans"]
                    if span["text"].strip().isdigit()
                    and span["bbox"][0] < NUMBER_MARGIN
                    and "Italic" in span["font"]
                    and int(span["text"]) in firsts
                )
    where.append((0, 0.0, 1))
    crowded = set()
    for page_no, y, _ in [*pdfcheck.chord_collisions(pdf), *pdfcheck.sign_chord_hits(pdf)]:
        same_page = [item for item in where if item[0] == page_no]
        crowded.add(min(same_page, key=lambda item: abs(item[1] - y))[2] if same_page else last_before(where, page_no))
    for page_no, y, _ in pdfcheck.past_margin(pdf):
        above = [item for item in where if item[0] == page_no and item[1] <= y]
        same_page = above or [item for item in where if item[0] == page_no]
        crowded.add(max(same_page, key=operator.itemgetter(1))[2] if same_page else last_before(where, page_no))
    return crowded


def must_start_line(bar: Bar, previous: Bar | None) -> bool:
    """Tell whether the bar starts a new block of lines: a section mark or a lyric label starts one.

    Returns:
        Whether a section mark or a lyric label starts a new block of lines here.

    """
    return previous is None or bool(bar.rehearsal or bar.segno or bar.coda or previous.final or piano.label_of(bar))


def sections(score: Score) -> list[list[int]]:
    """Bar indexes of each block; a tie into a section mark keeps the bars together.

    Returns:
        The bar indexes of each block.

    """
    blocks: list[list[int]] = []
    for index, bar in enumerate(score.bars):
        previous = score.bars[index - 1] if index else None
        if must_start_line(bar, previous) and (previous is None or not tied_out(previous)):
            blocks.append([])
        blocks[-1].append(index)
    return blocks


def short_line_problems(lines: list[list[int]]) -> list[str]:
    """Refuse a finished sheet whose lines of 2 bars or fewer pass the widest share any song is allowed.

    Returns:
        The refusal, empty when the share is within the widest allowed.

    """
    share = short_line_share(lines)
    return (
        [f"{share:.0%} of lines hold 2 bars or fewer, past {RELAXED_SHORT_SHARE:.0%}"]
        if share > RELAXED_SHORT_SHARE
        else []
    )


def short_line_share(lines: list[list[int]]) -> float:
    """Share of lines with 2 bars or fewer.

    Returns:
        The share of lines with 2 bars or fewer.

    """
    return sum(1 for line in lines if len(line) <= SHORT_LINE_BARS) / len(lines)


def balanced_pages(heights: list[float], pages: int) -> list[int]:
    """Indexes of the lines that start pages 2 and up, keeping the tallest page as short as possible.

    Returns:
        The indexes of the lines that start pages 2 and up.

    """
    count = len(heights)
    pages = min(pages, count)
    prefix = [0.0]
    for height in heights:
        prefix.append(prefix[-1] + height)
    best = {(0, 0): (0.0, -1)}
    for used in range(1, pages + 1):
        for end in range(used, count + 1):
            options = [
                (max(best[used - 1, start][0], prefix[end] - prefix[start]), start)
                for start in range(used - 1, end)
                if (used - 1, start) in best
            ]
            if options:
                best[used, end] = min(options)
    starts = []
    end = count
    for used in range(pages, 0, -1):
        end = best[used, end][1]
        starts.append(end)
    return sorted(start for start in starts if start > 0)


def line_heights(pdf: pathlib.Path, firsts: list[int]) -> tuple[list[float], int]:
    """Each line's height from where its bar number prints, the last on a page ending at the page's ink.

    Returns:
        Each line's height, and the number of pages.

    Raises:
        DoesNotFitError: A page is blank, so its lines have no height.

    """
    where: dict[int, tuple[int, float]] = {}
    doc = pdftext.open(pdf)
    bottoms = []
    tops = []
    for page_no, page in enumerate(doc):
        inked, _ = pdfcheck.inked_rows(page.page, 72)
        if not inked:
            msg = f"{pdf.name}: page {page_no + 1} is blank, so its lines have no height"
            raise DoesNotFitError(msg)
        tops.append(float(inked[0]))
        bottoms.append(float(inked[-1]))
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    if span["text"].strip().isdigit() and span["bbox"][0] < NUMBER_MARGIN and "Italic" in span["font"]:
                        where.setdefault(int(span["text"]), (page_no, span["bbox"][1]))
    where[1] = (0, tops[0])
    heights = []
    for index, first in enumerate(firsts):
        page_no, y = where[first]
        following = where.get(firsts[index + 1]) if index + 1 < len(firsts) else None
        heights.append(following[1] - y if following and following[0] == page_no else bottoms[page_no] - y)
    return heights, len(doc)


def balance(score: Score, probe: pathlib.Path) -> bool:
    """Spread the lines evenly over the pages the render needs, replacing every earlier page break.

    Returns:
        False when nothing changes.

    """
    firsts = [row[0] for row in rows(score)]
    heights, pages = line_heights(probe, firsts)
    wanted = {firsts[start] for start in balanced_pages(heights, pages)} if pages > 1 else set()
    held = {bar.number for bar in score.bars if bar.new_page}
    for bar in score.bars:
        bar.new_page = bar.number in wanted
    return wanted != held


def page_problems(score: Score, probe: pathlib.Path) -> list[str]:
    """Refuse a sheet whose printed pages differ from the balanced split of its own line heights.

    Returns:
        The refusal, empty when the pages are balanced.

    """
    return balance_problems(probe, [row[0] for row in rows(score)])


def balance_problems(probe: pathlib.Path, firsts: list[int]) -> list[str]:
    """Refuse printed pages whose tallest page stands taller than the most even split of the lines' own heights allows.

    The lines are given by the bars that open each line.

    Returns:
        The refusal, empty when the tallest page is within the slack.

    """
    heights, pages = line_heights(probe, firsts)
    if pages < MIN_BALANCED_PAGES:
        return []
    starts = balanced_pages(heights, pages)
    best = max(sum(heights[a:b]) for a, b in zip([0, *starts], [*starts, len(heights)], strict=True))
    printed = lines_per_page(probe, firsts)
    edges = [sum(printed[:at]) for at in range(len(printed) + 1)]
    tallest = max(sum(heights[a:b]) for a, b in itertools.pairwise(edges))
    if tallest > best * (1 + PAGE_SLACK):
        wanted = [b - a for a, b in zip([0, *starts], [*starts, len(firsts)], strict=True)]
        return [f"pages hold {printed} lines, balanced {wanted}"]
    return []


def lines_per_page(pdf: pathlib.Path, firsts: list[int]) -> list[int]:
    """How many of the score's lines each page prints, by where each line's bar number prints.

    Returns:
        How many of the score's lines each page prints.

    """
    pages = [page_of(pdf, first) for first in firsts]
    return [pages.count(page) for page in range(len(pdftext.open(pdf)))]


def page_of(pdf: pathlib.Path, bar: int) -> int:
    """Find the page a bar's printed number stands on; bar 1, which prints no number, opens page 1.

    Returns:
        The page a bar's printed number stands on.

    Raises:
        DoesNotFitError: The bar opens a line that prints no number.

    """
    if bar == 1:
        return 0
    for page_no, page in enumerate(pdftext.open(pdf)):
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    if span["text"].strip() == str(bar) and span["bbox"][0] < NUMBER_MARGIN and "Italic" in span["font"]:
                        return page_no
    msg = f"{pdf.name}: bar {bar} opens a line that prints no number"
    raise DoesNotFitError(msg)


class DoesNotFitError(Exception):
    """The size does not fit the song's page count."""


def styled(
    work: pathlib.Path,
    name: str,
    title: str,
    spatium: float,
    distance: float,
    *override_args: dict[str, object] | None,
    overrides: dict[str, object] | None = None,
) -> pathlib.Path:
    """Copy the style at 1 notation size and line gap, with the song title at the top right of every page.

    Any tag a kind of score sets its own way is set too.

    Returns:
        The path of the copy of the style.

    Raises:
        RuntimeError: The style has no single tag to set, or holds an override more than once.

    """
    if override_args:
        overrides = override_args[0]
    text = STYLE.read_text(encoding="utf-8")
    for tag, value in (
        ("Spatium", spatium),
        ("minSystemDistance", distance),
        ("maxSystemDistance", distance),
        ("oddHeaderR", title),
        ("evenHeaderR", title),
    ):
        text, count = re.subn(rf"<{tag}>[^<]*</{tag}>", f"<{tag}>{value}</{tag}>", text)
        if count != 1:
            msg = f"style has no single {tag}"
            raise RuntimeError(msg)
    for tag, setting in (overrides or {}).items():
        text, count = re.subn(rf"<{tag}>[^<]*</{tag}>", f"<{tag}>{setting}</{tag}>", text)
        if count > 1:
            msg = f"style holds {tag} {count} times"
            raise RuntimeError(msg)
        if not count:
            text = text.replace("</Style>", f"<{tag}>{setting}</{tag}></Style>", 1)
    text = text.replace(
        "</Style>",
        f"<chordStyle>custom</chordStyle><chordDescriptionFile>{chordnames.chord_file(work)}</chordDescriptionFile></Style>",
    )
    path = work / name
    fresh = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    fresh.write_text(text, encoding="utf-8")
    fresh.replace(path)
    return path


def boxed_sign(element: str) -> str:
    """1 Marker or Jump as its sign alone in its colored box; the jump it performs is untouched.

    Returns:
        The element as its sign alone in its colored box.

    """
    kind = "segno" if element.startswith("<Jump") or "<label>segno" in element else "coda"
    bare = re.sub(r"<offset[^>]*/>", "", element)
    bare = re.sub(r"<text>.*?</text>", f"<text><sym>{kind}</sym></text>", bare, flags=re.DOTALL)
    if "</style>" in bare:
        held = TARGET_HOLD if "repeat_left" in bare or "Repeat Text Left" in bare else ""
        return bare.replace("</style>", "</style>" + held + SIGN_BOX[kind], 1)
    return bare.replace(">", ">" + SIGN_BOX[kind], 1)


def beside_each_other(text: str) -> str:
    """In a bar holding 2 jump signs the Jump steps left, so both stay in the chord row of their own bar.

    Returns:
        The text with the Jump stepped left in every such bar.

    """
    measures = re.split(r"(?=<Measure)", text)
    for at, measure in enumerate(measures):
        if "<Jump>" in measure and "<label>coda</label>" in measure:
            stretched = re.sub(
                r"^<Measure([^>]*)>",
                lambda m: f"<Measure{m[1]}><stretch>{SIGN_BAR_STRETCH}</stretch>",
                measure,
                count=1,
            )
            measures[at] = stretched.replace(
                "<Jump>",
                f'<Jump><autoplace>0</autoplace><offset x="{SIGN_STEP}" y="{SIGN_RISE}"/>',
                1,
            )
    return "".join(measures)


def big_signs(mscz: pathlib.Path) -> None:
    """Every jump sign in its box, with no words."""
    with zipfile.ZipFile(mscz) as z:
        items = {info.filename: z.read(info.filename) for info in z.infolist()}
    for name, data in items.items():
        if name.endswith(".mscx"):
            items[name] = beside_each_other(
                re.sub(r"<(Marker|Jump)>.*?</\1>", lambda m: boxed_sign(m[0]), data.decode("utf-8"), flags=re.DOTALL),
            ).encode("utf-8")
    with zipfile.ZipFile(mscz, "w", zipfile.ZIP_DEFLATED) as z:
        for key, data in items.items():
            z.writestr(key, data)


def transpose_options(song: Song) -> dict[str, Any] | None:
    if not song.direction:
        return None
    return {
        "mode": "to_key",
        "direction": song.direction,
        "targetKey": song.fifths,
        "transposeKeySignatures": True,
        "transposeChordNames": True,
        "useDoubleSharpsFlats": False,
        "transposeInterval": 0,
    }


IMPORTS: dict[str, threading.Lock] = {}
IMPORTS_LOCK = threading.Lock()


def imported(score: Score, work: pathlib.Path, sheet: str, style: pathlib.Path, *, clear: bool) -> pathlib.Path:
    """1 import of a sheet with no line breaks, its signs set; every trial edits a copy of it.

    Returns:
        The path of the import.

    """
    flags = [(bar.new_system, bar.new_page) for bar in score.bars]
    for bar in score.bars:
        bar.new_system = bar.new_page = False
    text = musicxml.write(score)
    for bar, (line, page) in zip(score.bars, flags, strict=True):
        bar.new_system, bar.new_page = line, page
    whole = hashlib.sha256()
    for part in (
        text,
        style.read_text(encoding="utf-8"),
        str(clear),
        *(file.read_text(encoding="utf-8") for file in buildcache.sheet_code()),
    ):
        whole.update(part.encode("utf-8"))
    path = work / f"{sheet}-import-{whole.hexdigest()[:12]}.mscz"
    with IMPORTS_LOCK:
        lock = IMPORTS.setdefault(str(path), threading.Lock())
    with lock:
        if not path.exists():
            xml = path.with_suffix(".musicxml")
            xml.write_text(text, encoding="utf-8")
            fresh = path.with_name(path.stem + "-fresh.mscz")
            run("-S", str(style), "-o", str(fresh), str(xml))
            strip_title_frame(fresh)
            real_jumps(fresh, clear_chord=clear)
            big_signs(fresh)
            fresh.replace(path)
    return path


def staff_texts(text: str) -> str:
    """Every Expression an import made rewritten as staff text with the same placement.

    Expressions share 1 row and overprint, staff texts stack.

    Returns:
        The text with every Expression rewritten as staff text.

    """

    def rewritten(match: re.Match[str]) -> str:
        body = match[1]
        below = r"<direction>down</direction>" in body
        body = re.sub(r"\s*<direction>[^<]*</direction>", "", body)
        if "<placement>" not in body and below:
            body = "<placement>below</placement>" + body
        return f"<StaffText>{body}</StaffText>"

    return re.sub(r"<Expression>(.*?)</Expression>", rewritten, text, flags=re.DOTALL)


def edited(source: pathlib.Path, target: pathlib.Path, spatium: float, distance: float, score: Score) -> None:
    """Copy an import at 1 size and line gap, breaking lines and pages where the score says."""
    with zipfile.ZipFile(source) as old:
        items = {name: old.read(name) for name in old.namelist()}
    for name, data in items.items():
        if not name.endswith((".mss", ".mscx")):
            continue
        text = data.decode("utf-8")
        text = re.sub(r"<([Ss])patium>[^<]*</[Ss]patium>", lambda m: f"<{m[1]}patium>{spatium}</{m[1]}patium>", text)
        for tag in ("minSystemDistance", "maxSystemDistance"):
            text = re.sub(rf"<{tag}>[^<]*</{tag}>", f"<{tag}>{distance}</{tag}>", text)
        if name.endswith(".mscx"):
            text = staff_texts(text)
            text = re.sub(r"<LayoutBreak>.*?</LayoutBreak>\s*", "", text, flags=re.DOTALL)
            pieces = re.split(r"(?=<Measure)", text)
            count = 0
            for at, piece in enumerate(pieces):
                if piece.startswith("<Measure"):
                    count += 1
                    if count < len(score.bars) and score.bars[count].new_system:
                        kind = "page" if score.bars[count].new_page else "line"
                        pieces[at] = re.sub(
                            r"(</Measure>)",
                            rf"<LayoutBreak><subtype>{kind}</subtype></LayoutBreak>\1",
                            piece,
                            count=1,
                        )
            text = "".join(pieces)
        items[name] = text.encode("utf-8")
    fresh = target.with_name(target.stem + "-edit.mscz")
    with zipfile.ZipFile(fresh, "w", zipfile.ZIP_DEFLATED) as new:
        for name, data in items.items():
            new.writestr(name, data)
    fresh.replace(target)


def line_starts(score: Score) -> list[int]:
    return [row[0] - 1 for row in rows(score)]


MIN_TIED_CHAIN = 4


def tie_findings(starts: list[int], tied: list[bool]) -> tuple[list[str], list[str]]:
    """Fail a tie over a line end unless its tied run is too long for 1 line, which is only reported.

    Returns:
        The failures and the reported ties.

    """
    failures: list[str] = []
    reported: list[str] = []
    for start in starts:
        if start < TIE_LOOKBACK or start > len(tied) or not tied[start - 2]:
            continue
        low = start - 2
        while low > 0 and tied[low - 1]:
            low -= 1
        high = start - 2
        while high + 1 < len(tied) and tied[high + 1]:
            high += 1
        run = high - low + 2
        (reported if run >= MIN_TIED_CHAIN else failures).append(
            f"tie over line end into bar {start} (tied run of {run} bars)",
        )
    return failures, reported


def line_findings(score: Score, opening: set[int], bare: set[int]) -> list[str]:
    """Judge the line rules, 1 home for the layout and the gate.

    Opening holds the bars that start a line, bare those of them printed without a chord.

    Returns:
        The line rule findings.

    """
    marks = {bar.number for at, bar in enumerate(score.bars) if must_start_line(bar, score.bars[at - 1] if at else None)}
    tied_in = {bar.number for bar in score.bars if bar.number > 1 and tied_out(score.bars[bar.number - 2])}
    labels = {bar.number for bar in score.bars if piano.label_of(bar)}
    after_mark = {bar + 1 for bar in opening & marks if bar > 1}
    sourced = {bar.number for bar in score.bars if bar.source_line}
    lone = [f"bar {first}: lone bar on a line" for first in piano.lone_lines(sorted(opening), len(score.bars))]
    return (
        [f"bar {bar}: line starts without its chord" for bar in sorted(bare - marks - sourced)]
        + [f"bar {bar}: lyric label inside a line" for bar in sorted(labels - opening - tied_in - after_mark)]
        + lone
    )


def chord_starts(score_path: pathlib.Path, score: Score) -> list[str]:
    """Judge the line rules on a score file as written: its own line starts and the chords it prints.

    Returns:
        The line rule findings.

    """
    text = chordseat.score_text(score_path)[1]
    bare = set(chordseat.chordless_starts(text))
    opening = {numbers[0] for numbers in chordseat.bar_rows(text) if numbers}
    return line_findings(score, opening, bare)


def line_rule_reasons(score: Score, _variant: str = TONE) -> list[str]:
    """Judge the line rules on a plan, with the tie rule; the same findings the gate reads off the file.

    Returns:
        The line rule findings.

    """
    firsts = {row[0] for row in rows(score)}
    bare = {n for n in firsts if not score.bars[n - 1].chords}
    return line_findings(score, firsts, bare) + tie_findings(sorted(firsts), [tied_out(bar) for bar in score.bars])[0]


def line_rule_breaks(score: Score, variant: str = TONE) -> list[int]:
    """List the bars that break a line rule, each once.

    Returns:
        The bars that break a line rule, each once.

    """
    return sorted({bar_of(reason) for reason in line_rule_reasons(score, variant)})


def bar_of(reason: str) -> int:
    """Find the bar a line-rule reason names.

    Returns:
        The bar a line-rule reason names.

    Raises:
        ValueError: The reason names no bar.

    """
    found = re.search(r"bar (\d+)", reason)
    if found is None:
        msg = f"a line-rule reason names no bar: {reason}"
        raise ValueError(msg)
    return int(found[1])


def page_fill(pdf: pathlib.Path) -> float:
    """How far down its page the music reaches on the least filled page, as a share of the page height.

    Returns:
        The share of the page height the music reaches on the least filled page.

    """
    shares = []
    for page in pdftext.open(pdf):
        inked, height = pdfcheck.inked_rows(page.page, 36)
        shares.append((max(inked) + 1) / height if inked else 0.0)
    return min(shares, default=0.0)


def sizes_between(low: float, high: float) -> list[float]:
    return [round(low + step * SIZE_STEP, 2) for step in range(round((high - low) / SIZE_STEP) + 1)]


def lift_idle_cues(score: Score) -> None:
    """Move up a cue on a staff with no notes in its bar, or that staff could never hide."""
    for bar in score.bars:
        if not any(e.heads for v in bar.voices2 for e in v):
            bar.texts = [*bar.texts, *bar.texts2]
            bar.texts2 = []


def keep_pair(positions: pathlib.Path, pdf: pathlib.Path, target: pathlib.Path) -> None:
    """Keep a render's positions as the published PDF's pair, stamped with that PDF's digest."""
    shutil.copyfile(positions, target)
    target.with_suffix(".sha").write_text(hashlib.sha256(pdf.read_bytes()).hexdigest())


def pairs(positions: pathlib.Path, pdf: pathlib.Path) -> bool:
    """Whether kept positions were drawn by the render that printed exactly this PDF.

    Returns:
        Whether kept positions were drawn by the render that printed exactly this PDF.

    """
    stamp = positions.with_suffix(".sha")
    return positions.exists() and stamp.exists() and stamp.read_text() == hashlib.sha256(pdf.read_bytes()).hexdigest()


def paired(work: pathlib.Path, pdf_name: str) -> pathlib.Path:
    """Where the measure positions of the render that drew a published PDF are kept, outside the sheets folder.

    Returns:
        The path of the kept measure positions.

    """
    folder = work / "paired"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / pathlib.Path(pdf_name).with_suffix(".mpos").name
