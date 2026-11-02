"""Decode the Bravura vector sheet (MuseScore-engraved, treble-8 clef) into the score model."""
from __future__ import annotations

import re
from fractions import Fraction as F
from typing import Any

import pymupdf

from muse.model import LETTERS, Bar, Event, Head, Issue, Score, Text
from muse.symbolfont import parse_chord

BLACK, HALF = 0xF4BE, 0xF4BD
RESTS = {0xE4E5: F(1), 0xE4E6: F(1, 2), 0xE4E7: F(1, 4), 0xE4E4: F(2)}
BAR_REST = 0xE4E3
FLAG_UP = {0xE240: 1, 0xE242: 2}
FLAG_DOWN = {0xE241: 1, 0xE243: 2}
NOISE = ("Song G", "credit")


def decode(path: str) -> Score:
    page = pymupdf.open(path)[0]
    chars: list[tuple[int, float, float, float]] = []
    spans: list[tuple[str, float, float, float, float, str]] = []
    for block in page.get_text("rawdict")["blocks"]:
        for line in block.get("lines", []):
            for s in line["spans"]:
                if s["font"].startswith("Bravura"):
                    for ch in s["chars"]:
                        chars.append((ord(ch["c"]), ch["origin"][0], ch["origin"][1], s["size"]))
                else:
                    text = "".join(c["c"] for c in s["chars"])
                    if text.strip():
                        spans.append((text, s["chars"][0]["origin"][0], s["origin"][1], s["bbox"][2], s["size"], s["font"]))
    h_lines, v_thin, v_bar, fills = [], [], [], []
    ties = []
    for d in page.get_drawings():
        kinds = "".join(i[0] for i in d["items"])
        r = d["rect"]
        if kinds == "l":
            a, b = d["items"][0][1], d["items"][0][2]
            if abs(a.y - b.y) < 0.1 and r.width > 100:
                h_lines.append((a.y, min(a.x, b.x), max(a.x, b.x)))
            elif abs(a.x - b.x) < 0.1 and d["width"] > 0.9 and 18 < r.height < 22:
                v_bar.append((a.x, r.y0, r.y1))
            elif abs(a.x - b.x) < 0.1:
                v_thin.append((a.x, r.y0, r.y1))
        elif kinds == "llll" and d["type"] == "f":
            fills.append(r)
        elif kinds == "lllll":
            fills.append(r)  # tuplet bracket pieces share the rect list; filtered by shape
        elif kinds == "clcl":
            ties.append(r)
    ys = sorted({round(y, 1) for y, _, _ in h_lines})
    bases = [ys[i] for i in range(4, len(ys), 5)]
    step = (ys[4] - ys[0]) / 8
    size = next(c[3] for c in chars if c[0] == BLACK)
    bars: list[Bar] = []
    flags: list[Issue] = []
    prev = None
    length = F(2)
    for s_i, base in enumerate(bases):
        xs = sorted({round(x, 1) for x, y0, y1 in v_bar if abs(y1 - base) < 2})
        reps = [c for c in chars if c[0] in (0xE003, 0xE005) and abs(c[2] - base) < 25]
        for c in reps:
            xs.append(round(c[1] + (2 if c[0] == 0xE003 else 3), 1))
        xs = sorted(set(xs))
        merged: list[float] = []
        for x in xs:
            if not merged or x - merged[-1] > 5:
                merged.append(x)
        next((c[1] for c in chars if abs(c[2] - base) < 25 and 0xE080 <= c[0] <= 0xE089), None)
        left_edge = 58.0
        cols = [left_edge] + [m for m in merged if m > left_edge + 8]
        bases[s_i + 1] if s_i + 1 < len(bases) else base + 80
        bases[s_i - 1] if s_i else base - 80
        row_chars = [c for c in chars if base - 50 < c[2] < base + 22 and abs(c[3] - size) < 9]
        row_new: list[Bar] = []
        for a, b in zip(cols, cols[1:], strict=False):
            content = [c for c in row_chars if a <= c[1] < b - 0.5]
            if not any(c[0] in (BLACK, HALF, BAR_REST) or c[0] in RESTS for c in content):
                continue  # the gap between 2 strokes of 1 printed bar line
            bar = Bar(len(bars) + 1, 0, a, b, length)
            bar.new_system = not row_new and bool(bars)
            bar.base = base
            if not bars:
                bar.key = 1
                bar.time = (2, 4)
                bar.tempo = ("SlowRock", 56)
            bar.voices = read_bar(bar, content, base, step, size, v_thin, fills, flags)
            bar.repeat_start = any(c[0] == 0xE003 and abs(c[1] + 2 - a) < 7 for c in reps)
            bar.repeat_end = any(c[0] == 0xE005 and abs(c[1] + 3 - b) < 7 for c in reps)
            bars.append(bar)
            row_new.append(bar)
        if not row_new:
            continue
        for ch in chars:
            if base - 60 < ch[2] < base + 10 and ch[0] == 0xE047:
                near = min(row_new, key=lambda n: abs(n.left - ch[1]))
                if abs(near.left - ch[1]) < 16:
                    near.segno = True
                else:
                    target = next((n for n in row_new if n.left <= ch[1] < n.right + 2), row_new[-1])
                    target.dal_segno = True
            if base - 60 < ch[2] < base + 10 and ch[0] == 0xE048:
                target = next((n for n in row_new if n.left - 6 <= ch[1] < n.right), row_new[-1])
                if any(sp[0].strip().startswith("Coda") and abs(sp[2] - ch[2]) < 6 for sp in spans):
                    target.coda = True
                else:
                    target.to_coda = True
        row_spans = [t for t in spans if base - 60 < t[2] < base + 14]
        row_ch = [c for c in chars if c[0] in (0xE87B, 0xE873) and base - 60 < c[2] < base - 10]
        items = [t for t in row_spans if t[5] == "Academico-BoldItalic" and abs(t[4] - 10.6) < 0.3 and t[0].strip() and re.match(r"[A-G]", t[0].strip())]
        for t in row_spans:
            if t in items or not t[0].strip():
                continue
            text = t[0].strip()
            if text in NOISE or "=" in text or re.fullmatch(r"\d+", text) or text in ("A", "B"):
                if text in ("A", "B"):
                    target = next((n for n in row_new if n.left - 2 <= t[1] < n.right), row_new[0])
                    target.rehearsal.append(text)
                continue
            if re.fullmatch(r"[12]\.", text):
                target = next((n for n in row_new if n.left - 6 <= t[1] < n.right), row_new[-1])
                target.volta = text[0]
                continue
            if text in ("Coda", "To Coda"):
                continue
            target = next((n for n in row_new if n.left - 2 <= t[1] < n.right), row_new[-1])
            target.texts.append(Text(t[1], t[2], [(text, True, True)], True, size=9))
        taken: set[int] = set()
        for t in sorted(items, key=lambda t: t[1]):
            if id(t) in taken:
                continue
            text = t[0].strip()
            for c in row_ch:
                if c[0] == 0xE873 and 0 < c[1] - t[3] < 3 and abs(c[2] - t[2]) < 3:
                    text += "maj7"
            for c in row_ch:
                if c[0] == 0xE87B and 0 < c[1] - t[3] < 2:
                    follow = next((q for q in items if 0 < q[1] - c[1] < 8 and abs(q[2] - t[2]) < 2), None)
                    if follow:
                        text += "/" + follow[0].strip()
                        taken.add(id(follow))
            chord = parse_chord(text, prev, t[1])
            if chord is None:
                flags.append(Issue(row_new[0].number, f"unread chord {text!r}"))
                continue
            target = next((n for n in row_new if n.left - 4 <= t[1] < n.right - 2), row_new[-1])
            target.chords.append(chord)
            prev = chord
    wire_ties(bars, ties)
    bars[-1].final = True
    for i, bar in enumerate(bars):
        if bar.volta:
            j = i
            if bar.volta == "1":
                while j + 1 < len(bars) and not bars[j].repeat_end:
                    j += 1
            bars[j].volta_stop = bar.volta
    return Score(bars, [], flags, False, clef_octave=-1)


def read_bar(bar: Bar, content: list[tuple[int, float, float, float]], base: float, step: float, size: float, stems: list[Any], fills: list[Any], flags: list[Issue]) -> list[list[Event]]:
    events: list[Event] = []
    state: dict[tuple[str, int], int] = {}
    content = sorted(content, key=lambda c: c[1])
    for code, x, y, sz in content:
        grace = sz < 15
        if code in (BLACK, HALF):
            near_stems = [s for s in stems if -1 <= s[0] - x <= 7.5 and s[1] - 1 <= y <= s[2] + 1]
            stem = None
            level = 0
            beam = None
            nominal = F(1) if code == BLACK else F(2)
            if near_stems:
                st = near_stems[0]
                stem = "up" if st[0] - x > 3 else "down"
                tip = st[1] if stem == "up" else st[2]
                hits = [r for r in fills if r.x0 - 0.6 <= st[0] <= r.x1 + 0.6 and min(r.y0, r.y1) - 1.5 <= tip <= max(r.y0, r.y1) + 1.5 and r.width > 8]
                level = len({round((r.y0 + r.y1) / 2) for r in hits}) if len(hits) < 3 else 2
                if hits:
                    beam = id(hits[0])
                for f in content:
                    if f[0] in FLAG_UP and abs(f[1] - st[0]) < 1:
                        level = max(level, FLAG_UP[f[0]])
                    if f[0] in FLAG_DOWN and abs(f[1] - st[0]) < 1.5:
                        level = max(level, FLAG_DOWN[f[0]])
                nominal = nominal / 2**level if code == BLACK else nominal
            index = 2 + round((base - y) / step) + 4 * 7
            accidental = None
            for f in content:
                if 0 < x - f[1] < 11 and abs(f[2] - y) < 1.4 and f[0] in (0xE260, 0xE261, 0xE262):
                    accidental = {0xE260: -1, 0xE261: 0, 0xE262: 1}[f[0]]
            step_name, octave = LETTERS[index % 7], index // 7 - 1
            if accidental is not None:
                state[(step_name, octave)] = accidental
            head = Head(x, y, step_name, octave, None, "normal", alter=state.get((step_name, octave), 1 if step_name == "F" else 0))
            ev = Event(x, [head], F(1, 2) if grace else nominal, 0, stem, beam=beam, grace=grace)
            ev.fermata = any(f[0] in (0xE4C0, 0xE4C1) and abs(f[1] - x) < 7 for f in content)
            ev.accent = any(f[0] in (0xE4A0, 0xE4A1) and abs(f[1] - x) < 7 for f in content)
            events.append(ev)
        elif code in RESTS:
            events.append(Event(x, [], RESTS[code]))
        elif code == BAR_REST:
            pass
    events.sort(key=lambda e: (e.x))
    merged: list[Event] = []
    for ev in events:
        for other in merged:
            if abs(other.x - ev.x) < 1.2 and other.heads and ev.heads and not other.grace and not ev.grace:
                other.heads += ev.heads
                break
        else:
            merged.append(ev)
    for ev in merged:
        ev.heads.sort(key=lambda h: -h.y)
    if not merged:
        return [[Event(bar.left, [], bar.length, bar_rest=True)]]
    real = [e for e in merged if not e.grace]
    for _, tx, _, _ in [c for c in content if c[0] == 0xE883]:
        nearest = min(range(len(real)), key=lambda i: abs(real[i].x - tx))
        done = False
        for span in range(1, 5):
            for lo in range(max(0, nearest - span + 1), nearest + 1):
                window = real[lo : lo + span]
                if len(window) == span and not any(e.tuplet for e in window) and sum((e.nominal for e in window), F(0)) in (F(3, 2), F(3), F(3, 4)) and len(window) > 1:
                    for e in window:
                        e.tuplet = True
                    done = True
                    break
            if done:
                break
        if not done:
            flags.append(Issue(bar.number, "unplaced triplet"))
    t = F(0)
    total = sum((e.length for e in real), F(0))
    if total != bar.length:
        flags.append(Issue(bar.number, "bar reads " + str(total) + ": " + " ".join((("+".join(h.step + str(h.octave) for h in e.heads)) or "rest") + ":" + str(e.length) for e in real)))
    for e in merged:
        e.onset = t
        t += e.length
    return [merged]


def wire_ties(bars: list[Bar], ties: list[Any]) -> None:
    all_ev = [e for b in bars for v in b.voices for e in v if e.heads]
    for r in ties:
        starts = [e for e in all_ev if abs(e.x + 3 - r.x0) < 9 and any(abs(h.y - r.y0) < 16 for h in e.heads) and abs((e.x) - r.x0) < 10]
        rr = [e for e in all_ev if abs(e.x + 2 - r.x1) < 9 and e.x > r.x0 + 3 and any(abs(h.y - r.y1) < 16 for h in e.heads)]
        if starts and rr:
            a = min(starts, key=lambda e: abs(e.x - r.x0))
            b = min(rr, key=lambda e: abs(e.x - r.x1))
            pair = [(p, q) for p in a.heads for q in b.heads if (p.step, p.octave) == (q.step, q.octave)]
            if pair:
                pair[0][0].tie_start = True
                pair[0][1].tie_stop = True
            else:
                a.heads[0].slur_start = True
                b.heads[0].slur_stop = True
