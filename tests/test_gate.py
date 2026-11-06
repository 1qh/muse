"""Song derivation, refusals, the gate's own checks and the publishing guard."""

from fractions import Fraction as F

import pymupdf
import pytest

from muse import gate, pdfcheck, pdftext, port, publish, song, symbolfont
from muse.model import Bar, Score, UnknownEncoding, UnknownGlyph


def blank(path, width=595.0, height=842.0, pages=1, text=None):
    doc = pymupdf.open()
    for _ in range(pages):
        page = doc.new_page(width=width, height=height)
        page.insert_text((100, 400), text or "music", fontsize=10)
    doc.save(path)
    return path


@pytest.mark.parametrize(("root", "minor", "want"), [
    ("C", True, ("Am", 0, "down", -3)), ("D", True, ("Em", 1, "up", 2)), ("F", True, ("Em", 1, "down", -1)),
    ("E", True, ("Em", 1, "", 0)), ("A", True, ("Am", 0, "", 0)), ("G", True, ("Am", 0, "up", 2)),
    ("B", True, ("Am", 0, "down", -2)), ("D", False, ("C", 0, "down", -2)), ("A", False, ("G", 1, "down", -2)),
])
def test_target_is_the_nearest_key(root, minor, want):
    assert song.target_of(root, minor) == want


def test_name_and_printed_key_must_agree(tmp_path):
    path = blank(tmp_path / "Song-Cm.pdf", text="BIG TITLE")
    score = Score([Bar(1, 0, 0.0, 1.0, F(4), key=-1)], [], [])
    with pytest.raises(song.SongRefused, match="prints -1"):
        song.song_of(path, score)
    assert song.song_of(path, Score([Bar(1, 0, 0.0, 1.0, F(4), key=-3)], [], [])).tone == "Am"


def test_a_name_without_a_key_is_refused(tmp_path):
    with pytest.raises(song.SongRefused, match="must end in"):
        song.song_of(blank(tmp_path / "Song.pdf"))


def test_title_comes_from_the_largest_text_and_an_override_wins(tmp_path):
    path = tmp_path / "Song-Am.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((50, 50), "BIG TITLE", fontsize=30)
    page.insert_text((50, 90), "small credit", fontsize=9)
    doc.save(path)
    score = Score([Bar(1, 0, 0.0, 1.0, F(4), key=0)], [], [])
    assert song.song_of(path, score).title == "Big Title"
    (tmp_path / song.OVERRIDES).write_text('{"Song": "My Title"}', encoding="utf-8")
    assert song.song_of(path, score).title == "My Title"


def test_unknown_encoding_is_refused_by_font_name(tmp_path):
    with pytest.raises(UnknownEncoding, match="Helvetica"):
        song.encoding(blank(tmp_path / "Song-Am.pdf"))


def test_unknown_music_glyph_is_refused():
    class Page:
        def get_text(self, _kind):
            char = {"c": "", "origin": (1.0, 1.0)}
            return {"blocks": [{"lines": [{"spans": [{"font": "M", "size": 17.9, "chars": [char]}]}]}]}

    with pytest.raises(UnknownGlyph, match="0xf0aa"):
        symbolfont.load(Page(), {"M": "music"})


def test_any_page_count_passes_and_every_page_is_a4(tmp_path):
    assert gate.page_problems(blank(tmp_path / "ok.pdf", pages=2)) == []
    assert gate.page_problems(blank(tmp_path / "long.pdf", pages=5)) == []
    assert gate.page_problems(blank(tmp_path / "letter.pdf", width=612.0, height=792.0))[0].startswith("p1: not A4")
    assert gate.page_problems(blank(tmp_path / "wide.pdf", width=612.0))[0].startswith("p1: not A4")


def test_ink_near_any_edge_fails(tmp_path):
    assert gate.ink_problems(blank(tmp_path / "ok.pdf")) == []
    path = tmp_path / "top.pdf"
    doc = pymupdf.open()
    doc.new_page().draw_rect(pymupdf.Rect(100, 1, 200, 3), fill=(0, 0, 0))
    doc.save(path)
    assert "ink within" in gate.ink_problems(path)[0]


def test_tie_over_a_line_end_fails_unless_the_run_cannot_fit():
    tied = [False, True, False, False, False]
    assert gate.tie_findings([1, 3], tied)[0] == ["tie over line end into bar 3 (tied run of 2 bars)"]
    assert gate.tie_findings([1, 4], tied) == ([], [])
    assert gate.tie_findings([1, 4], [True, True, True, True, False])[1] == ["tie over line end into bar 4 (tied run of 5 bars)"]


def test_line_lengths_count_short_lines():
    assert gate.line_lengths([1, 3, 7], 8) == [2, 4, 2]


def test_homr_reading_counts_a_chord_once_and_skips_grace(tmp_path):
    path = tmp_path / "n.musicxml"
    path.write_text("<score><note><pitch><step>C</step></pitch></note><note><chord/><pitch><step>E</step></pitch></note><note><grace/><pitch><step>G</step></pitch></note><note><pitch><step>A</step></pitch></note></score>")
    assert gate.musicxml_letters(path) == ["E", "A"]


def test_agreement_names_the_bars_that_differ():
    share, bars = gate.agreement(["C", "D", "E", "F"], [1, 1, 2, 3], ["C", "D", "G", "F"])
    assert share == 0.75
    assert bars == [2]


def test_low_homr_agreement_fails(tmp_path, monkeypatch):
    src = blank(tmp_path / "Song-Am.pdf")
    reading = tmp_path / "r.musicxml"
    reading.write_text("<score><note><pitch><step>A</step></pitch></note></score>")
    monkeypatch.setattr(gate, "homr_command", lambda: "homr")
    monkeypatch.setattr(gate, "homr_reading", lambda *_: reading)
    from muse.model import Event, Head
    voice = [Event(0.0, [Head(0.0, 0.0, "C", 5)], F(1)) for _ in range(4)]
    score = Score([Bar(1, 0, 0.0, 1.0, F(4), voices=[voice])], [], [])
    assert gate.homr_findings(src, score, tmp_path)[0] == ["homr agrees on only 0% of notes"]


def test_missing_font_names_the_font_and_where_to_install(tmp_path):
    with pytest.raises(port.MissingFont, match=f"SF Pro Display.*{tmp_path}"):
        port.require_font((tmp_path,))
    for name in port.SF_PRO_FILES:
        (tmp_path / name).write_bytes(b"")
    port.require_font((tmp_path,))


def recorder(gate_fails=False, state="PRIVATE"):
    calls = []

    def run(*args, cwd=None):
        calls.append(args)
        if args[0] == "gh":
            return state
        if "muse.gate" in args and gate_fails:
            raise publish.Refused("failures 1")
        return "tree"

    return run, calls


def test_a_failing_song_keeps_its_folder_and_a_passing_song_publishes(tmp_path):
    calls = []

    def run(*args, cwd=None):
        calls.append(args)
        return {"view": "PRIVATE", "ls-files": "a/a.pdf", "--cached": "good/x.pdf", "rev-parse": "t", "get-url": "u"}.get(next((x for x in reversed(args) if x in ("view", "ls-files", "--cached", "rev-parse", "get-url")), ""), "PRIVATE" if "visibility" in args else "")

    blocked = publish.publish(tmp_path, tmp_path, tmp_path, run, lambda *_: {"good": [], "bad": ["bar 1 wrong"]})
    assert blocked == {"bad": ["bar 1 wrong"]}
    assert ("git", "add", "-A", "--", "good") in calls
    assert ("git", "add", "-A", "--", "bad") not in calls
    assert ("git", "checkout", "HEAD", "--", "bad") in calls


def test_sheets_repo_refuses_anything_but_sheets() -> None:
    assert publish.stray_files("a.pdf\nb.mscz\n") == []
    assert publish.stray_files("a.pdf\nnotes.md\n") == ["notes.md"]


def test_signs_must_stand_above_the_chords(tmp_path) -> None:
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


def test_small_sign_is_refused(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(pdfcheck, "sign_sizes", lambda path: ([10.0], 14.0))
    monkeypatch.setattr(pdftext, "open", lambda path: [])
    assert pdfcheck.sign_problems(tmp_path / "x.pdf") == ["sign of 10.0 under 1.6 times chords of 14.0"]
    monkeypatch.setattr(pdfcheck, "sign_sizes", lambda path: ([25.0], 14.0))
    assert pdfcheck.sign_problems(tmp_path / "x.pdf") == []


def test_words_printed_on_each_other_are_found(tmp_path) -> None:
    path = tmp_path / "overlap.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((100, 100), "BAND", fontsize=10)
    page.insert_text((105, 100), "Chorus", fontsize=10)
    page.insert_text((300, 100), "alone", fontsize=10)
    doc.save(path)
    assert [c[2] for c in pdfcheck.text_collisions(path)] == ["Chorus"]


def test_a_server_error_retries_and_a_real_refusal_does_not(tmp_path):
    seen = []

    def run(*args, cwd=None):
        seen.append(args)
        if len(seen) == 1:
            raise publish.Refused("remote rejected (Internal Server Error)")

    publish.pushed(run, ("git", "push"), tmp_path, wait=0)
    assert len(seen) == 2

    def refuse(*args, cwd=None):
        seen.append(args)
        raise publish.Refused("non-fast-forward")

    seen.clear()
    with pytest.raises(publish.Refused):
        publish.pushed(refuse, ("git", "push"), tmp_path, wait=0)
    assert len(seen) == 1


def test_an_unpushed_commit_is_pushed_even_with_nothing_new(tmp_path):
    calls = []

    def run(*args, cwd=None):
        calls.append(args)
        if "visibility" in args:
            return "PRIVATE"
        return "abc" if "@{u}..HEAD" in args else ""

    publish.publish(tmp_path, tmp_path, tmp_path, run, lambda *_: {})
    assert ("git", "push") in calls


def test_a_piano_label_may_follow_a_mark_that_opens_its_line(monkeypatch, tmp_path):
    from fractions import Fraction as F

    from muse import chordseat
    from muse.model import Bar, Chord, Score, Text

    def bar(number, **extra):
        return Bar(number, 0, 0.0, 100.0, F(4), voices=[], chords=[Chord(0.0, "A", 0, "")], **extra)

    score = Score([bar(1), bar(2, rehearsal=["B"]), bar(3, texts=[Text(0.0, 0.0, [("End: words", True, True)], False)]), bar(4)], [], [])
    monkeypatch.setattr(chordseat, "score_text", lambda path: (None, ""))
    monkeypatch.setattr(chordseat, "chordless_starts", lambda text: [])
    monkeypatch.setattr(chordseat, "bar_rows", lambda text: [[1], [2, 3, 4]])
    assert gate.chord_starts(tmp_path / "x-piano-final.mscz", score) == []
    assert gate.chord_starts(tmp_path / "x-original.mscz", score)


def test_a_publish_commit_names_its_songs():
    from muse import publish

    assert publish.commit_message(["AnhKhongThaThu", "CauHenCauThe"]) == "feat: publish AnhKhongThaThu, CauHenCauThe"
    assert publish.commit_message([]) == "chore: refresh sheets"


def test_a_purge_removes_only_sheet_versions_the_current_sheets_do_not_hold():
    from muse import publish

    tree = "100644 blob aaa\tSong/Song-Em.pdf\n100644 blob bbb\tSong/Song-Em.mscz\n"
    history = "c0mmit\naaa Song/Song-Em.pdf\nbbb Song/Song-Em.mscz\nold Song/Song-Em.pdf\nolder Other/Other-Am.mscz\nnotes README.md\n"
    assert publish.superseded(tree, history) == ["old", "older"]
