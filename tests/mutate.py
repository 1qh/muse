"""Mutation controls: plant each defect in a copy of the source and require the tests to go red."""
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
MUTATIONS = [
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
    ("scorefile.py", "if sorted(x.chords) != sorted(y.chords):", "if False:", "legacy chords"),
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
    ("piano.py", "if stop - first == 1]", "if False]", "lone bar found"),
    ("piano.py", "join_before if bar not in fixed else None,", "None,", "lone joins before"),
    ("port.py", "if len(piano.lone_lines(sorted(s + 1 for s in move), total)) < len(lone) and fits(move):", "if False:", "lone bar cleared"),
    ("buildcache.py", 'if not entry or entry.get("key") != key:', "if not entry:", "stale key"),
    ("buildcache.py", "return all(path.exists() and file_digest(path) == recorded[str(path)] for path in outputs)", "return True", "tampered output"),
    ("buildcache.py", "for part in (file_digest(source), song,", "for part in (song,", "source in key"),
    ("buildcache.py", "file_digest(style), tool_identity()", "tool_identity()", "style in key"),
    ("buildcache.py", '*(f"{path.name}={file_digest(path)}" for path in files)', "", "code in key"),
    ("port.py", "max(1, min(count, MAX_WORKERS, int(cores - load)))", "min(count, MAX_WORKERS)", "free workers"),
    ("piano.py", "if not noted & set(range(first, stop)) and", "if False and", "sparse piano lines"),
    ("piano.py", "<= MERGEABLE_LETTERS:", "<= 10**6:", "dense chords stay split"),
    ("port.py", "if at and load + cost > PIANO_LINE_LOAD and", "if at and load + cost > BARS_PER_LINE and", "piano line load"),
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
    ("port.py", "<= cap:\n            joins.add", "<= 0:\n            joins.add", "merge pass"),
    ("port.py", "if count - end == 1 and per_line > 1:", "if False:", "line grid"),
    ("port.py", "BARS_PER_LINE = 8", "BARS_PER_LINE = 4", "line length"),
    ("port.py", "if missing:", "if False:", "missing font"),
    ("song.py", "if printed != table[root]:", "if False:", "key disagreement"),
    ("song.py", "semis = (PITCH[target_root] - pitch + 6) % 12 - 6", "semis = PITCH[target_root] - pitch", "nearest target"),
    ("song.py", "raise UnknownEncoding(", "return 'symbolfont' or (", "unknown encoding"),
    ("song.py", "text.title() if text.isupper() else text", "text", "title case"),
    ("symbolfont.py", "if ord(ch[\"c\"]) not in KNOWN_GLYPHS:", "if False:", "unknown glyph"),
    ("publish.py", 'if visibility(out, run) != "PRIVATE":\n        raise Refused(f"{out} is not private', 'if False:\n        raise Refused(f"{out} is not private', "private only"),
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
    return subprocess.run(args, env=env, cwd=ROOT, capture_output=True, timeout=300).returncode


def main() -> None:
    survived = 0
    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp) / "src"
        shutil.copytree(ROOT / "src", base, ignore=shutil.ignore_patterns("*.egg-info", "__pycache__"))
        if run(base) != 0:
            raise SystemExit("baseline is red")
        for name, old, new, label in MUTATIONS:
            path = base / "muse" / name
            text = path.read_text()
            if old not in text:
                print(f"{label}: ANCHOR MISSING")
                survived += 1
                continue
            path.write_text(text.replace(old, new))
            code = run(base)
            path.write_text(text)
            print(f"{label}: {'red' if code else 'SURVIVED'}")
            survived += code == 0
    raise SystemExit(1 if survived else 0)


main()
