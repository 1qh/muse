"""Song derivation, refusals, the gate's own checks and the publishing guard."""

import concurrent.futures
import os
import pathlib
import shutil
import subprocess
import time
from fractions import Fraction
from typing import Never

import pymupdf
import pytest

from muse import build, chordseat, gate, pdfcheck, pdftext, port, publish, song, symbolfont
from muse.model import Bar, Chord, Event, Head, Score, Text, UnknownEncodingError, UnknownGlyphError


def blank(
    path: pathlib.Path,
    width: float = 595.0,
    height: float = 842.0,
    pages: int = 1,
    text: str | None = None,
) -> pathlib.Path:
    doc = pymupdf.open()
    for _ in range(pages):
        page = doc.new_page(width=width, height=height)
        page.insert_text((100, 400), text or "music", fontsize=10)
    doc.save(path)
    return path


@pytest.mark.parametrize(
    ("root", "minor", "want"),
    [
        ("C", True, ("Am", 0, "down", -3)),
        ("D", True, ("Em", 1, "up", 2)),
        ("F", True, ("Em", 1, "down", -1)),
        ("E", True, ("Em", 1, "", 0)),
        ("A", True, ("Am", 0, "", 0)),
        ("G", True, ("Am", 0, "up", 2)),
        ("B", True, ("Am", 0, "down", -2)),
        ("D", False, ("C", 0, "down", -2)),
        ("A", False, ("G", 1, "down", -2)),
    ],
)
def test_target_is_the_nearest_key(root: str, *, minor: bool, want: tuple[str, int, str, int]) -> None:
    assert song.target_of(root, minor=minor) == want


def test_name_and_printed_key_must_agree(tmp_path: pathlib.Path) -> None:
    path = blank(tmp_path / "Song-Cm.pdf", text="BIG TITLE")
    score = Score([Bar(1, 0, 0.0, 1.0, Fraction(4), key=-1)], [], [])
    with pytest.raises(song.SongRefusedError, match="prints -1"):
        song.song_of(path, score)
    assert song.song_of(path, Score([Bar(1, 0, 0.0, 1.0, Fraction(4), key=-3)], [], [])).tone == "Am"


def test_a_name_without_a_key_is_refused(tmp_path: pathlib.Path) -> None:
    with pytest.raises(song.SongRefusedError, match="must end in"):
        song.song_of(blank(tmp_path / "Song.pdf"))


def test_title_comes_from_the_largest_text_and_an_override_wins(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "Song-Am.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((50, 50), "BIG TITLE", fontsize=30)
    page.insert_text((50, 90), "small credit", fontsize=9)
    doc.save(path)
    score = Score([Bar(1, 0, 0.0, 1.0, Fraction(4), key=0)], [], [])
    assert song.song_of(path, score).title == "Big Title"
    (tmp_path / song.OVERRIDES).write_text('{"Song": "My Title"}', encoding="utf-8")
    assert song.song_of(path, score).title == "My Title"


def test_unknown_encoding_is_refused_by_font_name(tmp_path: pathlib.Path) -> None:
    with pytest.raises(UnknownEncodingError, match="Helvetica"):
        song.encoding(blank(tmp_path / "Song-Am.pdf"))


def test_unknown_music_glyph_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    char = {"c": "", "origin": (1.0, 1.0)}
    text = {"blocks": [{"lines": [{"spans": [{"font": "M", "size": 17.9, "chars": [char]}]}]}]}
    monkeypatch.setattr(pymupdf.Page, "get_text", lambda _page, _kind: text)
    doc = pymupdf.open()
    page = doc.new_page()

    with pytest.raises(UnknownGlyphError, match="0xf0aa"):
        symbolfont.load(page, {"M": "music"})


def test_any_page_count_passes_and_every_page_is_a4(tmp_path: pathlib.Path) -> None:
    assert pdfcheck.paper_problems(blank(tmp_path / "ok.pdf", pages=2)) == []
    assert pdfcheck.paper_problems(blank(tmp_path / "long.pdf", pages=5)) == []
    assert pdfcheck.paper_problems(blank(tmp_path / "letter.pdf", width=612.0, height=792.0))[0].startswith("p1: not A4")
    assert pdfcheck.paper_problems(blank(tmp_path / "wide.pdf", width=612.0))[0].startswith("p1: not A4")


def test_ink_near_any_edge_fails(tmp_path: pathlib.Path) -> None:
    assert pdfcheck.ink_problems(blank(tmp_path / "ok.pdf")) == []
    path = tmp_path / "top.pdf"
    doc = pymupdf.open()
    doc.new_page().draw_rect(pymupdf.Rect(100, 1, 200, 3), fill=(0, 0, 0))
    doc.save(path)
    assert "ink within" in pdfcheck.ink_problems(path)[0]


def test_tie_over_a_line_end_fails_unless_the_run_cannot_fit() -> None:
    tied = [False, True, False, False, False]
    assert port.tie_findings([1, 3], tied)[0] == ["tie over line end into bar 3 (tied run of 2 bars)"]
    assert port.tie_findings([1, 4], tied) == ([], [])
    assert port.tie_findings([1, 4], [True, True, True, True, False])[1] == [
        "tie over line end into bar 4 (tied run of 5 bars)",
    ]


def test_line_lengths_count_short_lines() -> None:
    assert gate.line_lengths([1, 3, 7], 8) == [2, 4, 2]


def test_homr_reading_counts_a_chord_once_and_skips_grace(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "n.musicxml"
    path.write_text(
        "<score><note><pitch><step>C</step></pitch></note><note><chord/><pitch><step>E</step></pitch></note><note><grace/><pitch><step>G</step></pitch></note><note><pitch><step>A</step></pitch></note></score>",
    )
    assert gate.musicxml_letters(path) == ["E", "A"]


def test_agreement_names_the_bars_that_differ() -> None:
    share, bars = gate.agreement(["C", "D", "E", "F"], [1, 1, 2, 3], ["C", "D", "G", "F"])
    assert share == pytest.approx(0.75)
    assert bars == [2]


def test_low_homr_agreement_fails(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = blank(tmp_path / "Song-Am.pdf")
    reading = tmp_path / "r.musicxml"
    reading.write_text("<score><note><pitch><step>A</step></pitch></note></score>")
    monkeypatch.setattr(gate, "homr_command", lambda: "homr")
    monkeypatch.setattr(gate, "homr_reading", lambda *_: reading)
    voice = [Event(0.0, [Head(0.0, 0.0, "C", 5)], Fraction(1)) for _ in range(4)]
    score = Score([Bar(1, 0, 0.0, 1.0, Fraction(4), voices=[voice])], [], [])
    assert gate.homr_findings(src, score, tmp_path)[0] == ["homr agrees on only 0% of notes"]


def test_missing_font_names_the_font_and_where_to_install(tmp_path: pathlib.Path) -> None:
    with pytest.raises(port.MissingFontError, match=f"SF Pro Display.*{tmp_path}"):
        port.require_font((tmp_path,))
    for name in port.SF_PRO_FILES:
        (tmp_path / name).write_bytes(b"")
    port.require_font((tmp_path,))


def test_a_failing_song_keeps_its_folder_and_a_passing_song_publishes(tmp_path: pathlib.Path) -> None:
    calls: list[tuple[str, ...]] = []

    def run(*args: str, cwd: pathlib.Path | None = None) -> str:
        _ = cwd
        calls.append(args)
        return {"view": "PRIVATE", "ls-files": "a/a.pdf", "--cached": "good/x.pdf", "rev-parse": "t", "get-url": "u"}.get(
            next((x for x in reversed(args) if x in {"view", "ls-files", "--cached", "rev-parse", "get-url"}), ""),
            "PRIVATE" if "visibility" in args else "",
        )

    blocked = publish.publish(
        publish.Places(tmp_path, tmp_path, tmp_path),
        run,
        lambda *_: {"good": [], "bad": ["bar 1 wrong"]},
    )
    assert blocked == {"bad": ["bar 1 wrong"]}
    assert ("git", "add", "-A", "--", "good") in calls
    assert ("git", "add", "-A", "--", "bad") not in calls
    assert ("git", "checkout", "HEAD", "--", "bad") in calls


def test_sheets_repo_refuses_anything_but_sheets() -> None:
    assert publish.stray_files("a.pdf\nb.mscz\n") == []
    assert publish.stray_files("a.pdf\nnotes.md\n") == ["notes.md"]


def test_signs_must_stand_above_the_chords(tmp_path: pathlib.Path) -> None:
    good = tmp_path / "good.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    font = pymupdf.Font("helv")
    page.insert_text((100, 100), "Am7", fontsize=10)
    writer = pymupdf.TextWriter(page.rect)
    writer.append((200, 100), "x", font=font, fontsize=20)
    writer.write_text(page)
    doc.save(good)
    assert pdfcheck.sign_problems(good) == []


def test_small_sign_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    monkeypatch.setattr(pdfcheck, "sign_sizes", lambda *_: ([10.0], 14.0))
    monkeypatch.setattr(pdftext, "read", lambda *_: [])
    assert pdfcheck.sign_problems(tmp_path / "x.pdf") == ["sign of 10.0 under 1.6 times chords of 14.0"]
    monkeypatch.setattr(pdfcheck, "sign_sizes", lambda *_: ([25.0], 14.0))
    assert pdfcheck.sign_problems(tmp_path / "x.pdf") == []


def test_words_printed_on_each_other_are_found(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "overlap.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((100, 100), "BAND", fontsize=10)
    page.insert_text((105, 100), "Chorus", fontsize=10)
    page.insert_text((300, 100), "alone", fontsize=10)
    doc.save(path)
    assert [c[2] for c in pdfcheck.text_collisions(path)] == ["Chorus"]


def test_a_server_error_retries_and_a_real_refusal_does_not(tmp_path: pathlib.Path) -> None:
    seen: list[tuple[str, ...]] = []

    def run(*args: str, cwd: pathlib.Path | None = None) -> None:
        _ = cwd
        seen.append(args)
        if len(seen) == 1:
            msg = "remote rejected (Internal Server Error)"
            raise publish.RefusedError(msg)

    def invoke(*args: str, cwd: pathlib.Path | None = None) -> str:
        run(*args, cwd=cwd)
        return ""

    publish.pushed(invoke, ("git", "push"), tmp_path, wait=0)
    assert len(seen) == 2

    def refuse(*args: str, cwd: pathlib.Path | None = None) -> Never:
        _ = cwd
        seen.append(args)
        msg = "non-fast-forward"
        raise publish.RefusedError(msg)

    seen.clear()
    with pytest.raises(publish.RefusedError):
        publish.pushed(refuse, ("git", "push"), tmp_path, wait=0)
    assert len(seen) == 1


def test_an_unpushed_commit_is_pushed_even_with_nothing_new(tmp_path: pathlib.Path) -> None:
    calls: list[tuple[str, ...]] = []

    def run(*args: str, cwd: pathlib.Path | None = None) -> str:
        _ = cwd
        calls.append(args)
        if "visibility" in args:
            return "PRIVATE"
        return "abc" if "@{u}..HEAD" in args else ""

    publish.publish(publish.Places(tmp_path, tmp_path, tmp_path), run, lambda *_: {})
    assert ("git", "push") in calls


def test_a_label_may_follow_a_mark_that_opens_its_line_on_any_sheet(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    def bar(number: int, rehearsal: list[str] | None = None, texts: list[Text] | None = None) -> Bar:
        return Bar(
            number,
            0,
            0.0,
            100.0,
            Fraction(4),
            voices=[],
            chords=[Chord(0.0, "A", 0, "")],
            rehearsal=rehearsal or [],
            texts=texts or [],
        )

    score = Score(
        [
            bar(1),
            bar(2),
            bar(3, rehearsal=["B"]),
            bar(4, texts=[Text(0.0, 0.0, [("End: words", True, True)], above=False)]),
            bar(5),
        ],
        [],
        [],
    )
    monkeypatch.setattr(chordseat, "score_text", lambda *_: (None, ""))
    monkeypatch.setattr(chordseat, "chordless_starts", lambda *_: [])
    monkeypatch.setattr(chordseat, "bar_rows", lambda *_: [[1, 2], [3, 4, 5]])
    assert port.chord_starts(tmp_path / "x-piano-final.mscz", score) == []
    assert port.chord_starts(tmp_path / "x-original.mscz", score) == []


def test_a_publish_commit_names_its_songs() -> None:
    assert publish.commit_message(["SongA", "SongB"]) == "feat: publish SongA, SongB"
    assert publish.commit_message([]) == "chore: refresh sheets"


def test_a_purge_removes_only_sheet_versions_the_current_sheets_do_not_hold() -> None:
    tree = "100644 blob aaa\tSong/Song-Em.pdf\n100644 blob bbb\tSong/Song-Em.mscz\n"
    history = (
        "c0mmit\naaa Song/Song-Em.pdf\nbbb Song/Song-Em.mscz\n"
        "old Song/Song-Em.pdf\nolder Other/Other-Am.mscz\nnotes README.md\n"
    )
    assert publish.superseded(tree, history) == ["old", "older"]


def test_a_bar_carrying_a_sign_stays_out_of_the_even_bars_judgement() -> None:
    bars = [
        "<Measure><voice/></Measure>",
        "<Measure><Marker><subtype>coda</subtype></Marker></Measure>",
        "<Measure><Jump><jumpTo>x</jumpTo></Jump></Measure>",
        "<Measure><voice/></Measure>",
    ]
    lower = "<Measure><Marker><subtype>segno</subtype></Marker></Measure>" * 4
    text = (
        f'<museScore><Score><Part></Part><Staff id="1">{"".join(bars)}</Staff>'
        f'<Staff id="2">{lower}</Staff></Score></museScore>'
    )
    assert chordseat.signed_bars(text) == {2, 3}


def test_a_sign_bar_leaves_both_the_evening_and_the_judgement_of_empty_bars() -> None:
    harmony = "<Harmony><harmonyInfo><root>14</root><name></name></harmonyInfo></Harmony>"
    bars = [
        f"<Measure><voice>{harmony}</voice></Measure>",
        f"<Measure><Marker><subtype>coda</subtype></Marker><voice>{harmony}</voice></Measure>",
        f"<Measure><voice>{harmony}</voice></Measure>",
    ]
    text = f'<museScore><Score><Part></Part><Staff id="1">{"".join(bars)}</Staff></Score></museScore>'
    line: chordseat.LineRecord = {"opening": 0.0, "bars": [30.0, 70.0, 100.0]}
    assert chordseat.empty_widths(text, [line], set()) == [(1, 30.0, 30.0), (3, 30.0, 30.0)]


@pytest.mark.parametrize("printing", [True, False])
def test_print_mode_alone_lets_a_bar_of_crowded_chords_out_of_the_even_bars_judgement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    *,
    printing: bool,
) -> None:
    seen = {}
    crowded = Bar(7, 0, 0.0, 100.0, Fraction(4), chords=[Chord(0.0, "D", 0, "m7"), Chord(50.0, "E", 0, "m7")])
    monkeypatch.setattr(gate.mode, "PRINT", printing)
    monkeypatch.setattr(gate, "chord_bound_bars", lambda *_: set())
    monkeypatch.setattr(gate, "decode", lambda *_: Score([crowded], [], []))
    monkeypatch.setattr(gate, "measured", lambda *_: tmp_path / "x.pdf")
    monkeypatch.setattr(gate.chordseat, "uneven", lambda _score, _pdf, noted: seen.setdefault("noted", noted) and [])
    gate.even_bars(tmp_path / "s", tmp_path / "s.mscz", tmp_path / "s.pdf", tmp_path, reduced=False)
    assert (7 in seen["noted"]) == printing


def test_a_bar_whose_chord_crowds_the_next_is_chord_bound_and_a_roomy_bar_is_not(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    line: chordseat.LineRecord = {
        "opening": 0.0,
        "bars": [36.0, 72.0, 113.6, 149.6],
        "size": 16.0,
        "lefts": [-1.0, 35.0, 71.0, 112.6],
        "rights": [22.0, 58.0, 102.3, 130.0],
    }
    monkeypatch.setattr(chordseat, "score_text", lambda *_: ("s.mscx", ""))
    monkeypatch.setattr(chordseat, "bar_rows", lambda *_: [[1, 2, 3, 4]])
    monkeypatch.setattr(chordseat, "rows_of", lambda *_: [[(0, True)]])
    monkeypatch.setattr(chordseat, "matched", lambda *_: [line])
    monkeypatch.setattr(chordseat, "CHORD_GAP", 0.5)
    monkeypatch.setattr(chordseat, "EQUAL_TOLERANCE", 4.0)
    assert gate.chord_bound_bars(tmp_path / "s.mscz", tmp_path / "s.pdf") == {3}


def test_print_mode_hands_chord_bound_bars_to_the_judgement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    seen = {}
    monkeypatch.setattr(gate.mode, "PRINT", True)
    monkeypatch.setattr(gate, "chord_bound_bars", lambda *_: {9})
    monkeypatch.setattr(gate, "decode", lambda *_: Score([], [], []))
    monkeypatch.setattr(gate, "measured", lambda *_: tmp_path / "x.pdf")
    monkeypatch.setattr(gate.chordseat, "uneven", lambda _score, _pdf, noted: seen.setdefault("noted", noted) and [])
    gate.even_bars(tmp_path / "s", tmp_path / "s.mscz", tmp_path / "s.pdf", tmp_path, reduced=False)
    assert 9 in seen["noted"]


def test_the_gate_judges_a_pdf_against_the_positions_of_its_own_render(tmp_path: pathlib.Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    pdf = tmp_path / "Song-C.pdf"
    pdf.write_bytes(b"%PDF drawn by the layout's render")
    drawn = tmp_path / "drawn.mpos"
    drawn.write_text("<positions of that render/>")
    port.keep_pair(drawn, pdf, port.paired(work, pdf.name))
    measured = gate.measured(pdf, work)
    assert measured.with_suffix(".mpos").read_text() == "<positions of that render/>"
    pdf.write_bytes(b"%PDF drawn by another render")
    with pytest.raises(port.DoesNotFitError):
        gate.measured(pdf, work)


def test_every_revision_publishing_asks_git_for_is_one_git_reads(tmp_path: pathlib.Path) -> None:
    """A fake runner answers any revision, so each one publishing passes is checked against a real repository."""
    calls: list[tuple[str, ...]] = []

    def run(*args: str, **_where: pathlib.Path) -> str:
        calls.append(args)
        return {"--cached": "staged", "rev-parse": "t", "get-url": "u"}.get(
            next((x for x in reversed(args) if x in {"--cached", "rev-parse", "get-url"}), ""),
            "",
        )

    publish.publish(publish.Places(tmp_path, tmp_path, tmp_path), run, lambda *_: {})
    revisions = [args[-1] for args in calls if args[:2] == ("git", "rev-parse")]
    assert revisions
    repo = tmp_path / "repo"
    repo.mkdir()
    git = shutil.which("git")
    assert git is not None
    for step in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "x"]):
        subprocess.run([git, "-C", str(repo), *step], check=True, capture_output=True)
    for revision in revisions:
        assert (
            subprocess.run([git, "-C", str(repo), "rev-parse", revision], capture_output=True, check=False).returncode == 0
        ), revision


def finished(name: str) -> tuple[dict[str, object] | None, str]:
    return {"song": name}, ""


def test_a_failed_publish_blocks_only_its_own_song(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """A publish that raises reports its song blocked and the run goes on to publish the rest."""

    def publish_song(*args: object) -> str:
        if args[-1] == "First":
            msg = "rev-parse: bad revision"
            raise publish.RefusedError(msg)
        return ""

    monkeypatch.setattr(build, "publish_song", publish_song)
    monkeypatch.setattr(build, "song_of", lambda src: type("Info", (), {"name": src.stem})())
    reports: list[dict[str, object]] = []
    refused: list[str] = []
    blocked: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        jobs = {pool.submit(finished, name): tmp_path / f"{name}.pdf" for name in ("First", "Second")}
        build._collect(pool, jobs, (tmp_path, tmp_path, tmp_path, tmp_path, True), (reports, refused, blocked))
    assert blocked == ["First"]
    assert len(reports) == 2


def crashed(name: str) -> tuple[dict[str, object] | None, str]:
    if name == "First":
        msg = "worker died"
        raise RuntimeError(msg)
    return {"song": name}, ""


def test_a_song_whose_build_raises_is_refused_and_the_run_goes_on(tmp_path: pathlib.Path) -> None:
    """A build that raises is that song's refusal, reported at once, and every other song is still collected."""
    reports: list[dict[str, object]] = []
    refused: list[str] = []
    blocked: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        jobs = {pool.submit(crashed, name): tmp_path / f"{name}.pdf" for name in ("First", "Second")}
        build._collect(pool, jobs, (tmp_path, tmp_path, tmp_path, tmp_path, False), (reports, refused, blocked))
    assert refused == ["First"]
    assert reports == [{"song": "Second"}]


SHELL = shutil.which("sh")
GROUP_REAP_SECONDS = 10.0
REAP_POLL_SECONDS = 0.05


def group_gone(group: int, within: float) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            os.killpg(group, 0)
        except ProcessLookupError:
            return True
        time.sleep(REAP_POLL_SECONDS)
    return False


def test_a_stopped_run_kills_its_song_workers_and_what_they_started() -> None:
    """Stopping the run ends every running worker's whole process group at once, never after it finishes."""
    assert SHELL is not None
    process = subprocess.Popen(
        [SHELL, "-c", "sleep 60 & echo started; wait"],
        start_new_session=True,
        stdout=subprocess.PIPE,
    )
    assert process.stdout is not None
    assert process.stdout.readline() == b"started\n"
    with build.RUNNING_LOCK:
        build.RUNNING.add(process)
    try:
        build.stop_children()
        assert process.wait(timeout=10) < 0
        assert group_gone(process.pid, GROUP_REAP_SECONDS)
    finally:
        with build.RUNNING_LOCK:
            build.RUNNING.discard(process)
