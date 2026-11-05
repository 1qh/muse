"""Mutation controls: plant each defect in a copy of the source and require the tests to go red."""
import concurrent.futures
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONTROL_WORKERS = 8
CONTROL_SECONDS = 180
MUTATIONS = [
    ("port.py", "seeded = near is not None and fits(near, BASE_DISTANCE)", "seeded = False", "search ignores saved size"),
    ("port.py", "or line_rule_breaks(laid) or chordless_opening(laid, base):", "or line_rule_breaks(laid):", "proof ignores chordless lines"),
    ("port.py", "or line_rule_breaks(laid) or chordless_opening(laid, base):", "or chordless_opening(laid, base):", "proof ignores line rules"),
    ("chordnames.py", 'f"<ChordList>{found[1]}</ChordList></Style>"', '"</Style>"', "chord list not embedded"),
    ("leland.py", "if r.width >= page.rect.width - 1:", "if False:", "page background is a beam"),
    ("leland.py", "and st[1] - 8 <= f[2] <= st[2] + 8:", ":", "flag of another staff"),
    ("leland.py", "return abs(stem_at[id(one)] - stem_at[id(other)]) < 0.3", "return abs(one.x - other.x) < DISPLACED", "chord by distance"),
    ("leland.py", "e.tuplet = True", "e.tuplet = False", "tuplet ignored"),
    ("leland.py", "bar.time = changed", "bar.time = None", "time change"),
    ("symbolfont.py", "or (piece[0].isalnum() and runs[-1][0][-1].isalnum()))", "or False)", "cue words joined"),
    ("oracle.py", "if count > decoded[word] and word not in excluded and word not in joined and not CHORD.fullmatch(word)}", "if False}", "lost word"),
    ("symbolfont.py", "any(abs(s.y - y) < CREDIT_ROW and s.x >= x for y, x in credit_rows)", "any(abs(s.y - y) < CREDIT_ROW for y, x in credit_rows)", "credit row left"),
    ("pdfcheck.py", "if not any(high <= head <= low for head in heads):", "if False:", "empty staff"),
    ("port.py", "bar.texts2 = []\n", "pass\n", "idle cue"),
    ("piano.py", "bar.voices = first if voices_sound(first) else second if voices_sound(second) else first", "bar.voices = first", "kept bar staff"),
    ("symbolfont.py", '" ".join(s.text for s in spans)', "spans[0].text", "tempo decimal"),
    ("piano.py", "sung.append((first, end - run if run >= MIN_TAIL else end))", "sung.append((first, first))", "coda tail"),
    ("scorefile.py", 'and marker.findtext("markerType") != label:', "and False:", "marker type"),
    ("piano.py", "stop = min(end, first + list(done.values())[-1]) if done else end", "stop = end", "closing section"),
    ("scorefile.py", 'holder.findtext("base") or holder.findtext("bass")', 'holder.findtext("base")', "bass tag"),
    ("musicxml.py", "if event.onset * 2 >= bar.length and abs(event.x - chord.x) <= EXACT_OVER_NOTE:", "if False:", "exact late chord"),
    ("musicxml.py", "if not has_heads(bar.voices) and not has_heads(bar.voices2):", "if False:", "silent bar rule"),
    ("musicxml.py", "at = round(wanted / step) * step", "at = wanted", "real beats"),
    ("musicxml.py", "return F(3, 2)", "return F(1)", "compound beat"),
    ("verify.py", "if got != sorted(want):", "if False:", "chord beat check"),
    ("scorefile.py", "if wanted not in symbols:", "if False:", "built-in sign"),
    ("scorefile.py", 'if marker.find("family") is not None:', "if False:", "forced sign font"),
    ("pdfcheck.py", "if len(heights) > 1 or len(sizes) > 1:", "if False:", "bass level"),
    ("pdfcheck.py", "if BASS_ALONE.match(text):", "if False:", "bass apart"),
    ("chordnames.py", '"<renderBase>/ :n :a</renderBase>"', '"<renderBase>m:-0.2:1 / m:0.2:1 :n :a m:0:-2</renderBase>"', "bass rule"),
    ("port.py", "return max(item for item in where if item[0] < page_no)[2]", "return -1", "numberless page"),
    ("port.py", 'if saved and float(saved["spatium"]) <= SIZE_FLOORS[0] and', "if False and", "floor gap"),
    ("chordnames.py", "CHORD_PATH.sub(STORED_NAME, data) if", "data if", "chord path dropped"),
    ("chordnames.py", "if m != STORED_NAME and", "if False and", "chord path found"),
    ("scorefile.py", "if same_spelling(x.chords) != same_spelling(y.chords):", "if False:", "legacy chords"),
    ("scorefile.py", "if sorted(x.pitches) != sorted(y.pitches):", "if False:", "legacy notes"),
    ("scorefile.py", "if sorted(x.signs) != sorted(y.signs):", "if False:", "legacy signs"),
    ("scorefile.py", "if sorted(x.jumps) != sorted(y.jumps):", "if False:", "legacy jumps"),
    ("scorefile.py", "scale = F(int(node.findtext", "_ = F(int(node.findtext", "saved triplet"),
    ("gate.py", 'return [f"missing {name}" for name in sorted(wanted - held)] + [f"stray {name}" for name in sorted(held - wanted)]', "return []", "folder files"),
    ("gate.py", "if held.pitches and number not in kept:", "if False:", "piano notes"),
    ("gate.py", 'problems = [f"{len(staves)} staves"] if len(staves) != 1 else []', "problems = []", "piano staves"),
    ("gate.py", 'if number not in kept and any(rest.findtext("visible") != "0"', 'if False and any(rest.findtext("visible") != "0"', "piano rests"),
    ("piano.py", "if label in done else end", "if False else end", "sibling length"),
    ("piano.py", "if held is None:\n            empty_bar(bar)", "if False:\n            empty_bar(bar)", "piano empties"),
    ("piano.py", "left -= len(taken)", "left -= 0", "hint length"),
    ("chordnames.py", '"maj7": "^7",', '"maj7": "\u03947",', "wrong chord mapping"),
    ("chordnames.py", "if heard.get(bar) != PLAYS[kind]]", "if False]", "playback check"),
    ("musicxml.py", "chordnames.stored(chord.kind)", "chord.kind", "stored spelling"),
    ("port.py", "max(empty, default=0.0) * len(empty)", "sum(empty)", "even room"),
    ("port.py", "or previous.final or piano.label_of(bar))", "or previous.final)", "label starts a line"),
    ("gate.py", 'for bar in sorted(labels - opening - tied_in)]', 'for bar in []]', "label inside a line"),
    ("port.py", "if following in starts and following not in fixed and len(row) > 1:", "if False:", "hand bar on"),
    ("port.py", "found = prove(src, work, variant, saved) if saved else None", "found = None", "saved layout ignored"),
    ("port.py", 'saved.get("searched", saved.get("rules")) if found and saved else layout_rules()', "layout_rules()", "owed search hidden"),
    ("port.py", "* size * len(bar.chords)", "* size", "shared bar room"),
    ("port.py", "while end < len(bars) and end - at < TRY_CEILING and line_room(bars[at:end + 1], spatium) <= LINE_WIDTH:", "while False:", "line fill"),
    ("port.py", "awkward = stop < len(bars) and (tied_out(bars[stop - 1]) or not bars[stop].chords or len(bars) - stop == 1)", "awkward = False", "awkward break"),
    ("port.py", "if bar == 0 or bar in chorded or bar in marks or bar not in settled:", "if True:", "chord line start"),
    ("chordseat.py", "if numbers and numbers[0] != 1 and not (chords and chords[0][0] == 0):", "if False:", "chordless start found"),
    ("port.py", 'if SEARCH_CAP["at"] and LAUNCHES["count"] >= SEARCH_CAP["at"]:', "if False:", "launch cap"),
    ("chordseat.py", "units = MPOS_UNITS if extent > LEGACY_EXTENT else LEGACY_MPOS_UNITS", "units = MPOS_UNITS", "position units"),
    ("chordseat.py", "return any(abs(x - left) < STEM_REACH or abs(x - right) < STEM_REACH for left, right in heads)", "return False", "stem as barline"),
    ("chordseat.py", "        if not is_signature(glyph[3]):\n            break\n", "        if not is_signature(glyph[3]):\n            continue\n", "signature run"),
    ("chordseat.py", "if abs(width - mean) > EQUAL_TOLERANCE]", "if False]", "uneven bars"),
    ("chordseat.py", "if gap is not None and (abs(gap - GAP) > TOLERANCE if reach is None", "if False and (abs(gap - GAP) > TOLERANCE if reach is None", "chord off its barline"),
    ("chordseat.py", "else not -TOLERANCE <= gap <= reach * space)]", "else False)]", "reach limit"),
    ("chordseat.py", 'edge = line["start"] if place == 0 else line["bars"][place - 1]', 'edge = line["start"]', "bar edge"),
    ("pdfcheck.py", "if spaces > MAX_CHORD_SPACES]", "if False]", "chord too high"),
    ("piano.py", "if stop - first == 1]", "if False]", "lone bar found"),
    ("piano.py", "join_before if bar not in fixed else None,", "None,", "lone joins before"),
    ("port.py", "if len(piano.lone_lines(sorted(s + 1 for s in move), total)) < len(lone) and fits(move):", "if False:", "lone bar cleared"),
    ("buildcache.py", 'if not entry or entry.get("key") != key:', "if not entry:", "stale key"),
    ("buildcache.py", "return all(path.exists() and file_digest(path) == recorded[str(path)] for path in outputs)", "return True", "tampered output"),
    ("buildcache.py", "for part in (file_digest(source), song,", "for part in (song,", "source in key"),
    ("buildcache.py", "file_digest(style), tool_identity()", "tool_identity()", "style in key"),
    ("buildcache.py", '*(f"{path.name}={file_digest(path)}" for path in files)', "", "code in key"),
    ("port.py", "max(1, min(count, MAX_WORKERS, int(cores - load)))", "min(count, MAX_WORKERS)", "free workers"),
    ("piano.py", "if bold:  # a band cue", "if False:  # a band cue", "shared cue"),
    ("symbolfont.py", 'return bool(re.fullmatch(r"[.,;:…·\'`-]+", text))', "return False", "print dust"),
    ("pdfcheck.py", "+ sign_on_chord(path) +", "+", "sign on chord"),
    ("piano.py", "left = HINT_WORDS  # a cue can stack", "pass  # a cue can stack", "stacked hint"),
    ("piano.py", "bar.voices2 = []", "pass", "second staff"),
    ("song.py", 'return {self.score_file, self.legacy_file, self.original_pdf, self.tone_pdf, self.piano_pdf}', 'return {self.score_file, self.legacy_file, self.original_pdf, self.piano_pdf}', "folder set"),
    ("symbolfont.py", "if y - last > BEAM_GAP:", "if True:", "beam pieces"),
    ("symbolfont.py", "if any((x in a) == (y in a) for x, y in close):", "if False:", "voice split apart"),
    ("pdfcheck.py", "if size < chords * SIGN_OVER_CHORD", "if False", "sign size"),
    ("pdfcheck.py", "if wide > (-JUMP_GAP if jump else TEXT_OVERLAP) and tall >", "if False and tall >", "text on text"),
    ("symbolfont.py", "steps = round((base - 0.2 - g.y) / step)", "steps = round((base - 0.2 - g.y) / step) + 1", "pitch"),
    ("symbolfont.py", "nominal = nominal * F(3, 2)", "nominal = nominal", "dot"),
    ("symbolfont.py", "0xF0EE: F(2)", "0xF0EE: F(4)", "half rest"),
    ("symbolfont.py", "level = beam_levels(", "level = 0 * beam_levels(", "beam"),
    ("symbolfont.py", "return (best[1], best[2]) if best else None", "return None", "voice split"),
    ("symbolfont.py", 'return Chord(x, m[1], alter[m[2]], m[3], m[4], alter[m[5] or ""])', "return Chord(x, m[1], 0, m[3], m[4], 0)", "chord accidental"),
    ("symbolfont.py", "x < b.right - 11", "x < b.right - 4", "chord bar"),
    ("symbolfont.py", "ev.heads = list({(h.step, h.octave): h for h in ev.heads}.values())", "pass", "doubled head"),
    ("symbolfont.py", "id(max(hits, key=lambda b: b.width))", "id(min(hits, key=lambda b: abs((b.y0 + b.y1) / 2 - tip)))", "beam group"),
    ("bravura.py", "e.tuplet = True", "e.tuplet = False", "triplet"),
    ("bravura.py", '1 if step_name == "F" else 0', "0", "key signature"),
    ("navigate.py", 'if bar.volta == "1" and second_time:', "if False:", "volta order"),
    ("navigate.py", "after_jump = False  # a repeat", "after_jump = True  # a repeat", "coda repeat"),
    ("pdfcheck.py", "y1 > height * BOTTOM_BAND", "y1 > height * 2", "margin band"),
    ("pdfcheck.py", "MIN_CHORD_GAP = 0.75", "MIN_CHORD_GAP = 0.0", "chord gap"),
    ("pdfcheck.py", "if len(hits) != 1:", "if len(hits) > 99:", "title"),
    ("pdfcheck.py", 'if not name.startswith(MUSIC_FONTS + ("SFPro", "SF-Pro", "SF Pro"))', "if False", "font"),
    ("pdfcheck.py", "and not CHORD.fullmatch(text)", "and False", "chord biggest"),
    ("gate.py", "if len(doc) > port.MAX_PAGES:", "if len(doc) > 99:", "page cap"),
    ("gate.py", "abs(page.rect.width - A4[0]) > A4_TOLERANCE", "False", "a4 width"),
    ("gate.py", "rows[0] < EDGE or", "False or", "ink top"),
    ("gate.py", "run >= MIN_TIED_CHAIN", "run >= 0", "tie allowed"),
    ("gate.py", "share < MIN_HOMR_AGREEMENT", "False", "homr floor"),
    ("gate.py", "letters[-1] = step", "letters.append(step)", "homr chord"),
    ("port.py", "if fits(lines[n] + lines[n + 1]):", "if False:", "merge pass"),
    ("port.py", "if missing:", "if False:", "missing font"),
    ("song.py", "if printed != table[root]:", "if False:", "key disagreement"),
    ("song.py", "semis = (PITCH[target_root] - pitch + 6) % 12 - 6", "semis = PITCH[target_root] - pitch", "nearest target"),
    ("song.py", "raise UnknownEncoding(", "return 'symbolfont' or (", "unknown encoding"),
    ("song.py", "text.title() if text.isupper() else text", "text", "title case"),
    ("symbolfont.py", "if ord(ch[\"c\"]) not in KNOWN_GLYPHS:", "if False:", "unknown glyph"),
    ("publish.py", "        keep_last_good(out, song, run)\n", "        pass\n", "keep last good"),
    ("publish.py", "if song not in blocked:", "if True:", "failing song staged"),
    ("publish.py", 'if attempt == tries or "Internal Server Error" not in str(error):', "if True:", "server error retry"),
    ("publish.py", 'elif not run("git", "rev-list", "@{u}..HEAD", cwd=out):', "else:", "unpushed commit"),
    ("preflight.py", "if done.returncode:", "if False:", "code check ignored"),
    ("preflight.py", "if not piano.parts(score):", "if False:", "no piano parts"),
    ("port.py", "if fixed_problems:", "if False:", "fixed problem searched"),
    ("verify.py", "12 * (octave + 1)", "12 * octave", "midi"),
    ("musicxml.py", "DIV = 48", "DIV = 24", "duration"),
    ("musicxml.py", 'return "major" if not kind else "other"', 'return "major"', "chord kind"),
]


def run(src: pathlib.Path) -> int:
    env = dict(os.environ, PYTHONPATH=str(src))
    args = [sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", "-o", f"pythonpath={src}", str(ROOT / "tests")]
    try:
        return subprocess.run(args, env=env, cwd=ROOT, capture_output=True, timeout=CONTROL_SECONDS).returncode
    except subprocess.TimeoutExpired:  # a planted bug that hangs the suite is a bug the suite noticed
        return 1


def control(mutation: tuple[str, str, str, str]) -> str:
    """1 planted bug in its own copy of the code, so controls run side by side."""
    name, old, new, label = mutation
    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp) / "src"
        shutil.copytree(ROOT / "src", base, ignore=shutil.ignore_patterns("*.egg-info", "__pycache__"))
        path = base / "muse" / name
        text = path.read_text()
        if old not in text:
            return f"{label}: ANCHOR MISSING"
        path.write_text(text.replace(old, new))
        return f"{label}: {'red' if run(base) else 'SURVIVED'}"


def main() -> None:
    if "--anchors" in sys.argv:
        dead = [name for file, old, _, name in MUTATIONS if old not in (ROOT / "src" / "muse" / file).read_text(encoding="utf-8")]
        print(f"dead anchors: {dead}" if dead else "ok")
        raise SystemExit(1 if dead else 0)
    with concurrent.futures.ThreadPoolExecutor(max_workers=CONTROL_WORKERS) as pool:
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp) / "src"
            shutil.copytree(ROOT / "src", base, ignore=shutil.ignore_patterns("*.egg-info", "__pycache__"))
            if run(base) != 0:
                raise SystemExit("baseline is red")
        outcomes = list(pool.map(control, MUTATIONS))
    for outcome in outcomes:
        print(outcome)
    raise SystemExit(1 if any(not outcome.endswith(": red") for outcome in outcomes) else 0)


main()
