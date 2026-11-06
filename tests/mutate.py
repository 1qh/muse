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
CONTROL_SECONDS = 240  # the fast suite; a control planted in code MuseScore runs gets HEAVY_SECONDS
HEAVY_SECONDS = 1500
HEAVY = ("extension/", "layout.py", "launch.py", "render.py")  # code only the MuseScore-driven tests exercise
MUTATIONS = [
    ("chordseat.py", "            step = (bottom - top) / 4 if bottom is not None else guess", "            step = guess", "staff spacing from 1 pair"),
    ("chordseat.py", '    edges = [line["opening"], *line["bars"]]', '    edges = [line["start"], *line["bars"]]', "sign counted as bar width"),
    ("layout.py", '            if "text on text" not in str(error):\n                raise', '            if True:\n                raise', "line gap never widened"),
    ("build.py", "buildcache.code_files(skip=skip), mode.label())", "buildcache.code_files(skip=skip))", "print build reused as strict"),
    ("pdfcheck.py", 'if span["text"].strip() and (x0 < box.x0 or y0 < box.y0 or x1 > box.x1 or y1 > box.y1):', 'if span["text"].strip() and (x0 < box.x0 or x1 > box.x1):', "ink above the page passes"),
    ("extension/main.js", "            item.text.offsetY = item.text.offsetY - rise  // placement", "            item.text.autoplace = false\n            item.text.offsetY = item.text.offsetY - rise  // placement", "raised text leaves no room"),
    ("layout.py", "return port.imported(self.score, self.work, self.name, self.import_style, False)", "return port.imported(self.score, self.work, self.name, self.style, False)", "1 import per size"),
    ("extension/main.js", "        placed[item.key].push({ x0: item.x0, x1: item.x1, top: top })\n", "\n", "raised texts overprint"),
    ("port.py", "    for page_no, y, _ in pdfcheck.past_margin(pdf):", "    for page_no, y, _ in [*pdfcheck.past_margin(pdf), *pdfcheck.text_collisions(pdf)]:", "text overlap replans"),
    ("extension/main.js", "    raiseText(all)\n", "\n", "cue left in the chord row"),
    ("layout.py", 'sorted(fit, key=lambda c: (c["lines"], -c["size"]))', 'sorted(fit, key=lambda c: -c["size"])', "piano sized for big notes"),
    ("extension/main.js", 'return page + ":" + Math.round(measure.pagePos.y)', 'return "0:" + Math.round(measure.pagePos.y)', "lines keyed without their page"),
    ("extension/main.js", "        if (lack > 0) {", "        if (false) {", "chord shortfall ignored"),
    ("extension/main.js", "return item.index < INK.length ?", "return false ?", "ink widths ignored"),
    ("layout.py", 'home = extensions_root() / f"{EXTENSION_FOLDER}-{digest}"', "home = extensions_root() / EXTENSION_FOLDER", "versions share 1 install"),
    ("chordseat.py", 'zip(line["lefts"], line["rights"], strict=True)', 'zip(line["chords"], line["rights"], strict=True)', "ink width from ink left"),
    ("layout.py", "spaces = [chordseat.space_points(score_path)] * len(gaps)", "spaces = chordseat.spaces_of(rows, lines)", "space measured off the PDF"),
    ("build.py", "return sorted(sources, key=lambda src: (rank.get(song_of(src).name, len(rank)), song_of(src).name))", "return sorted(sources, key=lambda src: song_of(src).name)", "priority ignored"),
    ("layout.py", "for share in (port.MAX_SHORT_SHARE, port.RELAXED_SHORT_SHARE):", "for share in (port.MAX_SHORT_SHARE,):", "choose never widens the share"),
    ("layout.py", 'for c in fit if c["fill"] >= port.PAGE_FILL)', "for c in fit)", "choose ignores fill"),
    ("layout.py", "        elif len(line) == 2:\n            cost += SHORT_COST", "        elif False:\n            cost += SHORT_COST", "plan ignores short lines"),
    ("layout.py", "        if len(line) == 1 and not lone_allowed and used < cap * LONE_FILL:\n            cost += LONE_COST", "        if False:\n            cost += LONE_COST", "plan strands lone bars"),
    ("layout.py", "+ (not ends_block and tied[line[-1]])", "", "plan breaks a tie"),
    ("layout.py", "+ (not opens_block and not chorded[line[0]])", "", "plan opens on a chordless bar"),
    ("layout.py", "if previous is not None and len(previous) == 1 and piano.label_of(score.bars[block[0]]):", "if False:", "piano label never second"),
    ("layout.py", "cost = ((cap - used) / cap) ** 2 if used <= cap else 0.0", "cost = 0.0", "plan ignores evenness"),
    ("layout.py", "widest = max((widths[i] for i in line if empty[i]), default=0.0)\n        return sum(widest if empty[i] else", "widest = 0.0\n        return sum(widest if False else", "plan ignores evened empty bars"),
    ("pdfcheck.py", "        top = chordseat.chord_size(path)\n        ordered", "        top = max(c[0] for c in chars)\n        ordered", "collisions sized by largest"),
    ("buildcache.py", 'CODE_SUFFIXES = (".py", ".js", ".json")', 'CODE_SUFFIXES = (".py",)', "extension outside build key"),
    ("layout.py", "    if seated and not score_path.exists():\n", "    if False:\n", "seated edits silently rebuilt"),
    ("chordseat.py", "return shaped.most_common(1)[0][0] if shaped else None", "return max(shaped) if shaped else None", "chord size by largest"),
    ("chordseat.py", "    if size is None:\n        raise ValueError", "    if False:\n        raise ValueError", "chordless page judged"),
    ("pdfcheck.py", "if any(overlap(box, letter) > COVER_SLACK for letter in letters)]", "if False]", "sign over chord unchecked"),
    ("port.py", 'f"<Measure{m[1]}><stretch>{SIGN_BAR_STRETCH}</stretch>"', 'f"<Measure{m[1]}>"', "2-sign bar not widened"),
    ("pdfcheck.py", "if y < min(row, default=below[0][0]) - ROW_SLACK:", "if False:", "sign row unchecked"),
    ("port.py", '"repeat_left" in bare or "Repeat Text Left" in bare else ""', 'False else ""', "target not held"),
    ("port.py", 'measures[at] = measure.replace("<Jump>",', 'measures[at] = measure.replace("<None>",', "signs stacked"),
    ("song.py", "if events and max(events) < chord.x - PAST_EVENT and", "if False and", "chord stays past its bar"),
    ("song.py", "chord.x + name * CHORD_LETTER > bar.right + CROSSING", "True", "chord moved without crossing"),
    ("piano.py", 'found = {bar.number for bar in score.bars if any(event.accent', 'found = {bar.number for bar in score.bars if False and any(event.accent', "accent dropped"),
    ("piano.py", "if later.chords or later.rehearsal or later.final:", "if True:", "fill stops at its cue"),
    ("port.py", 'return bare.replace(">", ">" + SIGN_BOX[kind], 1)', "return bare", "jump without box"),
    ("scorefile.py", 'if element.findtext("frameType") != "1" or box is None:', "if False:", "box check off"),
    ("gate.py", "sorted(labels - opening - tied_in - after_mark)", "sorted(labels - opening - tied_in)", "label after mark refused"),
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
    ("port.py", "or previous.final or piano.label_of(bar))", "or previous.final)", "label starts a line"),
    ("gate.py", 'for bar in sorted(labels - opening - tied_in - after_mark)]', 'for bar in []]', "label inside a line"),
    ("chordseat.py", "if numbers and numbers[0] != 1 and not (chords and chords[0][0] == 0):", "if False:", "chordless start found"),
    ("chordseat.py", "units = MPOS_UNITS if extent > LEGACY_EXTENT else LEGACY_MPOS_UNITS", "units = MPOS_UNITS", "position units"),
    ("chordseat.py", "return any(abs(x - left) < STEM_REACH or abs(x - right) < STEM_REACH for left, right in heads)", "return False", "stem as barline"),
    ("chordseat.py", "        if not is_signature(glyph[3]):\n            break\n", "        if not is_signature(glyph[3]):\n            continue\n", "signature run"),
    ("chordseat.py", "if abs(width - mean) > EQUAL_TOLERANCE]", "if False]", "uneven bars"),
    ("chordseat.py", "if gap is not None and (abs(gap - GAP) > TOLERANCE if reach is None", "if False and (abs(gap - GAP) > TOLERANCE if reach is None", "chord off its barline"),
    ("chordseat.py", "else not -TOLERANCE <= gap <= reach * space)]", "else False)]", "reach limit"),
    ("chordseat.py", 'edge = line["start"] if place == 0 else line["bars"][place - 1]', 'edge = line["start"]', "bar edge"),
    ("pdfcheck.py", "if spaces > MAX_CHORD_SPACES]", "if False]", "chord too high"),
    ("piano.py", "if stop - first == 1]", "if False]", "lone bar found"),
    ("buildcache.py", 'if not entry or entry.get("key") != key:', "if not entry:", "stale key"),
    ("buildcache.py", "return all(path.exists() and file_digest(path) == recorded[str(path)] for path in outputs)", "return True", "tampered output"),
    ("buildcache.py", "for part in (file_digest(source), song,", "for part in (song,", "source in key"),
    ("buildcache.py", "file_digest(style), tool_identity()", "tool_identity()", "style in key"),
    ("buildcache.py", '*(f"{path.name}={file_digest(path)}" for path in files)', "", "code in key"),
    ("build.py", "max(1, min(count, port.MAX_WORKERS, int(cores - load)))", "min(count, port.MAX_WORKERS)", "free workers"),
    ("piano.py", "if bold:  # a band cue", "if False:  # a band cue", "shared cue"),
    ("symbolfont.py", 'return bool(re.fullmatch(r"[.,;:…·\'`-]+", text))', "return False", "print dust"),
    ("pdfcheck.py", "+ sign_on_chord(path) +", "+", "sign on chord"),
    ("piano.py", "left = HINT_WORDS  # a cue can stack", "pass  # a cue can stack", "stacked hint"),
    ("piano.py", "bar.voices2 = []", "pass", "second staff"),
    ("song.py", 'return {self.score_file, self.original_pdf, self.tone_pdf, self.piano_pdf} |', 'return {self.score_file, self.original_pdf, self.piano_pdf} |', "folder set"),
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
    ("chordseat.py", "CHORD_GAP = mode.pick(0.75, 0.5)", "CHORD_GAP = mode.pick(0.0, 0.0)", "chord gap"),
    ("pdfcheck.py", "if len(hits) != 1:", "if len(hits) > 99:", "title"),
    ("pdfcheck.py", 'if not name.startswith(MUSIC_FONTS + ("SFPro", "SF-Pro", "SF Pro"))', "if False", "font"),
    ("pdfcheck.py", "and not CHORD.fullmatch(text)", "and False", "chord biggest"),
    ("gate.py", "abs(page.rect.width - A4[0]) > A4_TOLERANCE", "False", "a4 width"),
    ("gate.py", "rows[0] < EDGE or", "False or", "ink top"),
    ("gate.py", "run >= MIN_TIED_CHAIN", "run >= 0", "tie allowed"),
    ("gate.py", "share < MIN_HOMR_AGREEMENT", "False", "homr floor"),
    ("gate.py", "letters[-1] = step", "letters.append(step)", "homr chord"),
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
    ("verify.py", "12 * (octave + 1)", "12 * octave", "midi"),
    ("musicxml.py", "DIV = 48", "DIV = 24", "duration"),
    ("musicxml.py", 'return "major" if not kind else "other"', 'return "major"', "chord kind"),
]


def run(src: pathlib.Path, heavy: bool = False) -> int:
    env = dict(os.environ, PYTHONPATH=str(src))
    args = [sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", "-o", f"pythonpath={src}", str(ROOT / "tests")] + ([] if heavy else ["-m", "not musescore"])
    try:
        return subprocess.run(args, env=env, cwd=ROOT, capture_output=True, timeout=HEAVY_SECONDS if heavy else CONTROL_SECONDS).returncode
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
        return f"{label}: {'red' if run(base, name.startswith(HEAVY)) else 'SURVIVED'}"


def main() -> None:
    if "--anchors" in sys.argv:
        dead = [name for file, old, _, name in MUTATIONS if old not in (ROOT / "src" / "muse" / file).read_text(encoding="utf-8")]
        print(f"dead anchors: {dead}" if dead else "ok")
        raise SystemExit(1 if dead else 0)
    chosen = MUTATIONS
    if "--only" in sys.argv:  # prove named controls alone, each label after the flag
        wanted = set(sys.argv[sys.argv.index("--only") + 1:])
        chosen = [m for m in MUTATIONS if m[3] in wanted]
        if len(chosen) != len(wanted):
            raise SystemExit(f"unknown controls: {sorted(wanted - {m[3] for m in chosen})}")
    with concurrent.futures.ThreadPoolExecutor(max_workers=CONTROL_WORKERS) as pool:
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp) / "src"
            shutil.copytree(ROOT / "src", base, ignore=shutil.ignore_patterns("*.egg-info", "__pycache__"))
            env = dict(os.environ, PYTHONPATH=str(base))
            args = [sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", "-o", f"pythonpath={base}", str(ROOT / "tests")]
            try:
                done = subprocess.run(args, env=env, cwd=ROOT, capture_output=True, text=True, timeout=HEAVY_SECONDS)
            except subprocess.TimeoutExpired:
                raise SystemExit(f"baseline did not finish in {CONTROL_SECONDS} seconds") from None
            if done.returncode:
                raise SystemExit(f"baseline is red: {(done.stdout + done.stderr)[-1500:]}")
        outcomes = list(pool.map(control, chosen))
    for outcome in outcomes:
        print(outcome)
    raise SystemExit(1 if any(not outcome.endswith(": red") for outcome in outcomes) else 0)


main()
