"""Port 1 source PDF: decode, write MusicXML, create the MuseScore file, transpose, export."""
from __future__ import annotations

import hashlib
import os
import pathlib
import re
import subprocess
import threading
import zipfile
from collections.abc import Callable
from typing import Any

import pymupdf

from muse import buildcache, chordnames, chordseat, musicxml, pdfcheck, pdftext, piano
from muse.model import Bar, Score
from muse.song import Song, decode

CHORD_GAP = 0.75  # of the chord size, between 2 chords
CHORD_SIZE_PER_SPACE = 8.0  # printed chord points per unit of the style's spatium
SIZE_FLOORS = (1.2, 1.15)  # below 1.2 only for a song that fits its pages no other way; under 1.15 the fixed title outgrows chords
SIZE_CAP = 2.0  # the largest notation any sheet takes
PAGE_FILL = 0.85  # how far down every page the music must reach
MAX_WORKERS = 10
SEAT_PASSES = 5
SIZE_STEP = 0.05
CLIMB_START, CLIMB_STEP, CLIMB_STEPS = 1.3, 0.1, 8
SIGN_CLEARANCE = 3.0  # spaces a chord moves right off a sign printed on its baseline
TARGET_HOLD = '<autoplace>0</autoplace><offset x="1" y="-3"/>'
SIGN_STEP = -6  # spaces a jump sign steps left of the sign beside it
SIGN_RISE = -2.5  # spaces above the staff, in its own chord row
SIGN_BAR_STRETCH = 2.5  # a bar holding 2 signs widens to hold both
SIGN_SIZE = 25  # a sign may exceed the chords: about 2.5 times the default sign
MARKER_TYPES = {"segno": "segno", "coda": "codab"}
LEGACY_SIGN_SIZE = 25  # MuseScore 3 sizes the sign as text
LEGACY_ONLY_FONTS = ("staffFontFace", "systemFontFace", "beginFontFace", "continueFontFace", "endFontFace")  # its names for text styles
MAX_SHORT_SHARE = 0.15  # readers find many 2-bar lines sparse; smaller notation is preferred
RELAXED_SHORT_SHARE = 0.30  # when no size holds a song to the base share
LEGACY_TOP_BORDER = 15  # MuseScore 3 lifts jump words and voltas into the title otherwise
BASE_DISTANCE = 11.0
ORIGINAL = "original"
TONE = "tone"
PIANO = "piano"
STYLE = pathlib.Path(__file__).with_name("style.mss")
SF_PRO_FILES = tuple(f"SF-Pro-Display-{face}.otf" for face in ("Regular", "RegularItalic", "Bold", "BoldItalic"))
FONT_DIRS = (pathlib.Path.home() / "Library" / "Fonts", pathlib.Path("/Library/Fonts"))


class MissingFont(RuntimeError):
    """MuseScore would quietly fall back to another font."""


def require_font(folders: tuple[pathlib.Path, ...] = FONT_DIRS) -> None:
    missing = [name for name in SF_PRO_FILES if not any((folder / name).exists() for folder in folders)]
    if missing:
        raise MissingFont(f"SF Pro Display is missing {missing}; install it from Apple into {folders[0]}")


LAUNCHES = {"count": 0}
SEARCH_CAP = {"at": 0, "hit": 0}  # launch count where a sheet's search stops, 0 leaving it open; and how often a cap stopped it


def run(*args: str) -> None:
    if SEARCH_CAP["at"] and LAUNCHES["count"] >= SEARCH_CAP["at"]:
        SEARCH_CAP["hit"] += 1
        raise DoesNotFit("the search reached its launch cap")
    LAUNCHES["count"] += 1
    done = subprocess.run(["mscore", *args], capture_output=True, text=True, timeout=240)
    if done.returncode:
        raise RuntimeError(f"mscore {args}: {done.stderr[-400:]}")


JUMPS = {
    "To Coda": "<Marker><style>repeat_right</style><text><sym>coda</sym></text><label>coda</label><markerType>coda</markerType><markerCenterOnSymbol>0</markerCenterOnSymbol><musicSymbolSize>25</musicSymbolSize></Marker>",
    "D.S. al Coda": "<Jump><musicSymbolSize>25</musicSymbolSize><text><sym>segno</sym></text><jumpTo>segno</jumpTo><playUntil>coda</playUntil><continueAt>codab</continueAt></Jump>",
}
BOX = '<frameType>1</frameType><frameWidth>0.45</frameWidth><framePadding>0.4</framePadding><frameFgColor r="{}" g="{}" b="{}" a="255"/>'
SIGN_BOX = {"segno": BOX.format(220, 0, 0), "coda": BOX.format(0, 0, 220)}


def real_jumps(mscz: pathlib.Path, clear_chord: bool = False) -> None:
    """Import keeps jump words as staff text; replace them with MuseScore's own jump elements."""
    items = {}
    with zipfile.ZipFile(mscz) as z:
        for info in z.infolist():
            items[info.filename] = z.read(info.filename)
    for name, data in items.items():
        if not name.endswith(".mscx"):
            continue
        text = data.decode("utf-8")
        for words, element in JUMPS.items():
            pattern = re.compile(r"\s*<StaffText>(?:(?!</StaffText>).)*?<text>" + re.escape(words) + r"</text>\s*</StaffText>", re.S)
            while True:
                m = pattern.search(text)
                if not m:
                    break
                start = text.rfind("<voice>", 0, m.start())
                text = text[:start] + element + text[start:m.start()] + text[m.end():]
        for sym, label in (("segno", "segno"), ("coda", "codab")):
            # a visible marker is both the printed sign and the jump target; the style sizes it
            pattern = re.compile(r"\s*<StaffText>(?:(?!</StaffText>).)*?<text><sym>" + sym + r"</sym></text>\s*</StaffText>", re.S)
            for m in reversed(list(pattern.finditer(text))):
                start = text.rfind("<voice>", 0, m.start())
                element = f"<Marker><style>repeat_left</style><text><sym>{sym}</sym></text><label>{label}</label><markerType>{MARKER_TYPES[sym]}</markerType><markerCenterOnSymbol>0</markerCenterOnSymbol><musicSymbolSize>{SIGN_SIZE}</musicSymbolSize></Marker>"
                text = text[:start] + element + text[start:m.start()] + text[m.end():]
                chord = text.find("<Harmony>", start)
                if clear_chord and chord != -1 and chord < text.find("</Measure>", start):
                    # a marker ignores its own offset in layout, so the chord beside it makes the room
                    at = chord + len("<Harmony>")
                    text = text[:at] + f'<offset x="{SIGN_CLEARANCE}" y="0"/>' + text[at:]
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
            items[name] = re.sub(r"\s*<VBox>(?:(?!</VBox>|<Text>).)*</VBox>", "", data.decode("utf-8"), flags=re.S).encode("utf-8")
    with zipfile.ZipFile(mscz, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in items.items():
            z.writestr(name, data)


def system_starts(pdf: pathlib.Path) -> list[int]:
    """The bar number printed at the start of every line, read from the exported PDF."""
    found = []
    for page in pdftext.open(pdf):
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    if span["text"].strip().isdigit() and span["bbox"][0] < 60 and "Italic" in span["font"]:
                        found.append(int(span["text"]))
    return found


def rows(score: Score) -> list[list[int]]:
    """1-based bar numbers of each declared row."""
    out: list[list[int]] = []
    for number, bar in enumerate(score.bars, 1):
        if not out or bar.new_system or bar.new_page:
            out.append([])
        out[-1].append(number)
    return out


def tied_out(bar: Bar) -> bool:
    """Whether a tie leaves the bar: only a voice's last event can carry one past the barline."""
    return any(head.tie_start for voice in [*bar.voices, *bar.voices2] if voice for head in voice[-1].heads)


def last_before(where: list[tuple[int, float, int]], page_no: int) -> int:
    """The line a page with no printed bar number continues: the last one numbered on an earlier page."""
    return max(item for item in where if item[0] < page_no)[2]


def crowded_rows(pdf: pathlib.Path, firsts: list[int]) -> set[int]:
    """First bar of every line holding chords that run together, matched by page and height."""
    where = []
    for page_no, page in enumerate(pdftext.open(pdf)):
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    if span["text"].strip().isdigit() and span["bbox"][0] < 60 and "Italic" in span["font"] and int(span["text"]) in firsts:
                        where.append((page_no, span["bbox"][1], int(span["text"])))
    where.append((0, 0.0, 1))
    crowded = set()
    for page_no, y, _ in pdfcheck.chord_collisions(pdf):
        same_page = [item for item in where if item[0] == page_no]
        crowded.add(min(same_page, key=lambda item: abs(item[1] - y))[2] if same_page else last_before(where, page_no))
    for page_no, y, _ in [*pdfcheck.past_margin(pdf), *pdfcheck.text_collisions(pdf)]:
        above = [item for item in where if item[0] == page_no and item[1] <= y]
        same_page = above or [item for item in where if item[0] == page_no]
        crowded.add(max(same_page, key=lambda item: item[1])[2] if same_page else last_before(where, page_no))
    return crowded


def must_start_line(bar: Bar, previous: Bar | None) -> bool:
    """A section mark or a lyric label starts a new block of lines."""
    return previous is None or bool(bar.rehearsal or bar.segno or bar.coda or previous.final or piano.label_of(bar))


def sections(score: Score) -> list[list[int]]:
    """Bar indexes of each block; a tie into a section mark keeps the bars together."""
    blocks: list[list[int]] = []
    for index, bar in enumerate(score.bars):
        previous = score.bars[index - 1] if index else None
        if must_start_line(bar, previous) and (previous is None or not tied_out(previous)):
            blocks.append([])
        blocks[-1].append(index)
    return blocks














def short_line_share(lines: list[list[int]]) -> float:
    """Share of lines with 2 bars or fewer."""
    return sum(1 for line in lines if len(line) <= 2) / len(lines)








def balanced_pages(heights: list[float], pages: int) -> list[int]:
    """Indexes of the lines that start pages 2 and up, keeping the tallest page as short as possible."""
    count = len(heights)
    pages = min(pages, count)
    prefix = [0.0]
    for height in heights:
        prefix.append(prefix[-1] + height)
    best = {(0, 0): (0.0, -1)}
    for used in range(1, pages + 1):
        for end in range(used, count + 1):
            options = [(max(best[(used - 1, start)][0], prefix[end] - prefix[start]), start)
                       for start in range(used - 1, end) if (used - 1, start) in best]
            if options:
                best[(used, end)] = min(options)
    starts = []
    end = count
    for used in range(pages, 0, -1):
        end = best[(used, end)][1]
        starts.append(end)
    return sorted(start for start in starts if start > 0)


def line_heights(pdf: pathlib.Path, firsts: list[int]) -> tuple[list[float], int]:
    """Each line's height from where its bar number prints, the last on a page ending at the page's ink."""
    where: dict[int, tuple[int, float]] = {}
    doc = pdftext.open(pdf)
    bottoms = []
    tops = []
    for page_no, page in enumerate(doc):
        pix = page.get_pixmap(dpi=72, colorspace=pymupdf.csGRAY)
        inked = [y for y in range(pix.height) if min(pix.samples[y * pix.width:(y + 1) * pix.width]) < 128]
        tops.append(float(inked[0]))
        bottoms.append(float(inked[-1]))
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    if span["text"].strip().isdigit() and span["bbox"][0] < 60 and "Italic" in span["font"]:
                        where.setdefault(int(span["text"]), (page_no, span["bbox"][1]))
    where[1] = (0, tops[0])
    heights = []
    for index, first in enumerate(firsts):
        page_no, y = where[first]
        following = where.get(firsts[index + 1]) if index + 1 < len(firsts) else None
        heights.append(following[1] - y if following and following[0] == page_no else bottoms[page_no] - y)
    return heights, len(doc)


def balance(score: Score, probe: pathlib.Path) -> bool:
    """Spread the lines evenly over the pages they need; False when there is only 1 page."""
    firsts = [row[0] for row in rows(score)]
    heights, pages = line_heights(probe, firsts)
    if pages < 2:
        return False
    for start in balanced_pages(heights, pages):
        score.bars[firsts[start] - 1].new_page = True
    return True




class DoesNotFit(Exception):
    """This size does not fit the song's page count."""


def styled(work: pathlib.Path, name: str, title: str, spatium: float, distance: float) -> pathlib.Path:
    """A copy of the style at 1 notation size and line gap, with the song title at the top right of every page."""
    text = STYLE.read_text(encoding="utf-8")
    for tag, value in (("Spatium", spatium), ("minSystemDistance", distance), ("maxSystemDistance", distance), ("oddHeaderR", title), ("evenHeaderR", title)):
        text, count = re.subn(rf"<{tag}>[^<]*</{tag}>", f"<{tag}>{value}</{tag}>", text)
        if count != 1:
            raise RuntimeError(f"style has no single {tag}")
    text = text.replace("</Style>", f"<chordStyle>custom</chordStyle><chordDescriptionFile>{chordnames.chord_file(work)}</chordDescriptionFile></Style>")
    path = work / name
    path.write_text(text, encoding="utf-8")
    return path


def boxed_sign(element: str) -> str:
    """1 Marker or Jump as its sign alone in its colored box; the jump it performs is untouched."""
    kind = "segno" if element.startswith("<Jump") or "<label>segno" in element else "coda"
    bare = re.sub(r"<offset[^>]*/>", "", element)
    bare = re.sub(r"<text>.*?</text>", f"<text><sym>{kind}</sym></text>", bare, flags=re.S)
    if "</style>" in bare:
        held = TARGET_HOLD if "repeat_left" in bare or "Repeat Text Left" in bare else ""  # a target held down in its own line's chord row
        return bare.replace("</style>", "</style>" + held + SIGN_BOX[kind], 1)
    return bare.replace(">", ">" + SIGN_BOX[kind], 1)


def beside_each_other(text: str) -> str:
    """In a bar holding 2 jump signs the Jump steps left, so both stay in the chord row of their own bar."""
    measures = re.split(r"(?=<Measure)", text)
    for at, measure in enumerate(measures):
        if "<Jump>" in measure and "<label>coda</label>" in measure:
            measure = re.sub(r"^<Measure([^>]*)>", lambda m: f"<Measure{m[1]}><stretch>{SIGN_BAR_STRETCH}</stretch>", measure, count=1)
            measures[at] = measure.replace("<Jump>", f'<Jump><autoplace>0</autoplace><offset x="{SIGN_STEP}" y="{SIGN_RISE}"/>', 1)
    return "".join(measures)


def big_signs(mscz: pathlib.Path) -> None:
    """Every jump sign in its box, with no words."""
    with zipfile.ZipFile(mscz) as z:
        items = {info.filename: z.read(info.filename) for info in z.infolist()}
    for name, data in items.items():
        if name.endswith(".mscx"):
            items[name] = beside_each_other(re.sub(r"<(Marker|Jump)>.*?</\1>", lambda m: boxed_sign(m[0]), data.decode("utf-8"), flags=re.S)).encode("utf-8")
    with zipfile.ZipFile(mscz, "w", zipfile.ZIP_DEFLATED) as z:
        for key, data in items.items():
            z.writestr(key, data)


def transpose_options(song: Song) -> dict[str, Any] | None:
    if not song.direction:
        return None
    return {"mode": "to_key", "direction": song.direction, "targetKey": song.fifths, "transposeKeySignatures": True, "transposeChordNames": True, "useDoubleSharpsFlats": False, "transposeInterval": 0}








def seat_chords(score_path: pathlib.Path, pdf: pathlib.Path, render: Callable[[], None], song: str, noted: set[int] | None = None, reach: float | None = None) -> None:
    """Even out empty bars where asked and move beat-1 chords to their barlines by measuring the render, until nothing is left to move."""
    try:
        for _ in range(SEAT_PASSES if noted is not None else 0):  # bars first: evening them moves every barline
            if not chordseat.equalize(score_path, pdf, noted or set()):
                break
            render()
        for _ in range(SEAT_PASSES):
            if not chordseat.align(score_path, pdf, reach):
                break
            render()
    except ValueError:
        return  # chords run together, so the text layer merges them; the crowding check splits that line






IMPORTS: dict[str, threading.Lock] = {}
IMPORTS_LOCK = threading.Lock()


def imported(score: Score, work: pathlib.Path, sheet: str, style: pathlib.Path, clear: bool) -> pathlib.Path:
    """1 import of a sheet with no line breaks, its signs set; every trial edits a copy of it."""
    flags = [(bar.new_system, bar.new_page) for bar in score.bars]
    for bar in score.bars:
        bar.new_system = bar.new_page = False
    text = musicxml.write(score)
    for bar, (line, page) in zip(score.bars, flags, strict=True):
        bar.new_system, bar.new_page = line, page
    whole = hashlib.sha256()
    for part in (text, style.read_text(encoding="utf-8"), str(clear), *(file.read_text(encoding="utf-8") for file in buildcache.code_files())):
        whole.update(part.encode("utf-8"))
    path = work / f"{sheet}-import-{whole.hexdigest()[:12]}.mscz"  # keyed on what it was built from, so a stale import is never reused
    with IMPORTS_LOCK:
        lock = IMPORTS.setdefault(str(path), threading.Lock())
    with lock:
        if not path.exists():
            xml = path.with_suffix(".musicxml")
            xml.write_text(text, encoding="utf-8")
            fresh = path.with_name(path.stem + "-fresh.mscz")
            run("-S", str(style), "-o", str(fresh), str(xml))
            strip_title_frame(fresh)
            real_jumps(fresh, clear)
            big_signs(fresh)
            fresh.replace(path)
    return path


def edited(source: pathlib.Path, target: pathlib.Path, spatium: float, distance: float, score: Score) -> None:
    """A copy of an import at 1 size and line gap, breaking lines and pages where the score says."""
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
            text = re.sub(r"<LayoutBreak>.*?</LayoutBreak>\s*", "", text, flags=re.S)
            pieces = re.split(r"(?=<Measure)", text)
            count = 0
            for at, piece in enumerate(pieces):
                if piece.startswith("<Measure"):
                    count += 1
                    if count < len(score.bars) and score.bars[count].new_system:
                        kind = "page" if score.bars[count].new_page else "line"
                        pieces[at] = re.sub(r"(</Measure>)", rf"<LayoutBreak><subtype>{kind}</subtype></LayoutBreak>\1", piece, count=1)
            text = "".join(pieces)
        items[name] = text.encode("utf-8")
    fresh = target.with_name(target.stem + "-edit.mscz")
    with zipfile.ZipFile(fresh, "w", zipfile.ZIP_DEFLATED) as new:
        for name, data in items.items():
            new.writestr(name, data)
    fresh.replace(target)






def line_starts(score: Score) -> list[int]:
    return [row[0] - 1 for row in rows(score)]




def line_rule_breaks(score: Score) -> list[int]:
    """Bars the line rules say start a line and that sit inside one, or lines opening on a bar without a chord that a mark does not excuse."""
    firsts = {row[0] for row in rows(score)}
    marks = {block[0] + 1 for block in sections(score)}
    inside = [bar.number for bar in score.bars if bar.number not in firsts and piano.label_of(bar) and not (bar.number > 1 and tied_out(score.bars[bar.number - 2]))]
    bare = [n for n in firsts if n not in marks and not score.bars[n - 1].chords]
    return sorted({*inside, *bare})










def page_fill(pdf: pathlib.Path) -> float:
    """How far down its page the music reaches on the least filled page, as a share of the page height."""
    shares = []
    for page in pdftext.open(pdf):
        pix = page.get_pixmap(dpi=36, colorspace=pymupdf.csGRAY)
        inked = [y for y in range(pix.height) if min(pix.samples[y * pix.width:(y + 1) * pix.width]) < 128]
        shares.append((max(inked) + 1) / pix.height if inked else 0.0)
    return min(shares, default=0.0)




def sizes_between(low: float, high: float) -> list[float]:
    return [round(low + step * SIZE_STEP, 2) for step in range(int(round((high - low) / SIZE_STEP)) + 1)]










def legacy_command() -> str:
    """MuseScore 3's own executable; it is frozen at its last release."""
    return os.environ.get("MUSE_MS3") or str(pathlib.Path.home() / "Applications" / "MuseScore 3.app" / "Contents" / "MacOS" / "mscore")


def build_legacy(src: pathlib.Path, work: pathlib.Path, target: pathlib.Path, spatium: float, distance: float, info: Song) -> None:
    """The MuseScore 3 file, from the same MusicXML as the latest file, in its own program's format."""
    score = decode(src)
    style = styled(work, f"{info.name}-legacy.mss", info.title, spatium, distance)
    fonts = "".join(f"<{tag}>SF Pro Display</{tag}>" for tag in LEGACY_ONLY_FONTS)
    style.write_text(style.read_text(encoding="utf-8").replace('version="4.70"', 'version="3.02"').replace("</Style>", fonts + "</Style>"), encoding="utf-8")
    style.write_text(re.sub(r"<staffUpperBorder>[^<]*</staffUpperBorder>", f"<staffUpperBorder>{LEGACY_TOP_BORDER}</staffUpperBorder>", style.read_text(encoding="utf-8")), encoding="utf-8")
    xml = work / f"{info.name}-{ORIGINAL}.musicxml"
    raw = work / f"{info.name}-legacy.mscz"
    done = subprocess.run([legacy_command(), "-S", str(style), "-o", str(raw), str(xml)], capture_output=True, text=True, timeout=240, stdin=subprocess.DEVNULL)
    if done.returncode:
        raise RuntimeError(f"MuseScore 3: {done.stderr[-300:]}")
    strip_title_frame(raw)
    legacy_signs(raw)
    big_signs(raw)
    legacy_sign_size(raw)
    chordnames.embed_chord_list(raw, chordnames.chord_file(work))
    seen = work / f"{info.name}-legacy-seat.pdf"

    def render() -> None:
        LAUNCHES["count"] += 1
        for out in (seen, seen.with_suffix(".mpos")):
            subprocess.run([legacy_command(), "-o", str(out), str(raw)], capture_output=True, check=True, timeout=240, stdin=subprocess.DEVNULL)

    render()
    seat_chords(raw, seen, render, f"{info.name}-legacy", {bar.number for bar in score.bars if piano.has_notes(bar)}, chordseat.LEGACY_REACH)
    target.write_bytes(raw.read_bytes())


def legacy_sign_size(mscz: pathlib.Path) -> None:
    """MuseScore 3 sizes a sign as text."""
    with zipfile.ZipFile(mscz) as z:
        items = {info.filename: z.read(info.filename) for info in z.infolist()}
    for name, data in items.items():
        if name.endswith(".mscx"):
            text = data.decode("utf-8")
            for sym in ("segno", "coda"):
                text = text.replace(f"<text><sym>{sym}</sym></text>", f"<size>{LEGACY_SIGN_SIZE}</size><text><sym>{sym}</sym></text>")
            items[name] = text.encode("utf-8")
    with zipfile.ZipFile(mscz, "w", zipfile.ZIP_DEFLATED) as z:
        for key, data in items.items():
            z.writestr(key, data)


def lift_idle_cues(score: Score) -> None:
    """A cue on a staff with no notes in its bar moves up, or that staff could never hide."""
    for bar in score.bars:
        if not any(e.heads for v in bar.voices2 for e in v):
            bar.texts = [*bar.texts, *bar.texts2]
            bar.texts2 = []


def legacy_signs(mscz: pathlib.Path) -> None:
    """MuseScore 3's import already writes the built-in signs; only their size is ours."""
    with zipfile.ZipFile(mscz) as z:
        items = {info.filename: z.read(info.filename) for info in z.infolist()}
    for name, data in items.items():
        if name.endswith(".mscx"):
            text = data.decode("utf-8")
            text = text.replace("<hideEmptyStaves>1</hideEmptyStaves>", "<hideEmptyStaves>1</hideEmptyStaves>\n    <dontHidStavesInFirstSystm>0</dontHidStavesInFirstSystm>", 1)  # MuseScore 3 spells it this way
            for spelled, legacy_name in chordnames.LEGACY_SPELLING.items():
                text = text.replace(f"<name>{spelled}</name>", f"<name>{legacy_name}</name>")
            items[name] = text.encode("utf-8")
    with zipfile.ZipFile(mscz, "w", zipfile.ZIP_DEFLATED) as z:
        for key, data in items.items():
            z.writestr(key, data)
