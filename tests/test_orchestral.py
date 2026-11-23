"""The orchestral path's own rules, proved without the private sources.

Its glyphs, its writer, its song identity, and how the build and gate send a full score its own way.
"""

import pathlib
import re
import xml.etree.ElementTree as ET
import zipfile
from fractions import Fraction
from typing import Never, TypedDict, Unpack, override

import pymupdf
import pytest

from muse import build, fullcheck, fullscore, gate, musicxml, orchestral, port, preflight, song, symbolfont
from muse.model import Bar, Event, Head, Orchestra, Part, Text, UnknownGlyphError


def test_every_full_score_glyph_is_known_with_its_meaning_and_none_reaches_a_lead_sheet() -> None:
    codes = set(symbolfont.ORCHESTRAL_GLYPHS)
    assert len(codes) == 21
    assert codes <= symbolfont.KNOWN_GLYPHS
    assert not codes & symbolfont.LEAD_SHEET_GLYPHS
    assert codes <= orchestral.MUSIC_MEANINGS | orchestral.EXTRA_MEANINGS | orchestral.TEXT_MEANINGS
    assert all(symbolfont.ORCHESTRAL_GLYPHS[code] for code in codes)


class FakePage(pymupdf.Page):
    """1 page holding 1 music glyph, nothing drawn."""

    def __init__(self, code: int) -> None:
        self.code = code

    @override
    def get_text(self, _kind: str) -> dict[str, object]:
        char = {"c": chr(self.code), "origin": (1.0, 1.0), "bbox": (1.0, 0.0, 3.0, 2.0)}
        return {
            "blocks": [
                {
                    "lines": [
                        {
                            "spans": [
                                {
                                    "font": "M",
                                    "size": 10.4,
                                    "chars": [char],
                                    "flags": 0,
                                    "bbox": (1.0, 0.0, 3.0, 2.0),
                                    "origin": (1.0, 1.0),
                                },
                            ],
                        },
                    ],
                },
            ],
        }

    @override
    def get_drawings(self, extended: bool = False) -> list[object]:
        return []

    @property
    @override
    def rect(self) -> Never:
        msg = "no drawing asks for the page box"
        raise AssertionError(msg)


def test_an_undecoded_glyph_is_refused_and_a_known_one_is_read() -> None:
    with pytest.raises(UnknownGlyphError, match="0xf0c3"):
        orchestral.read(FakePage(0xF0C3), {"M": "music"})
    with pytest.raises(UnknownGlyphError, match="0xf0aa"):
        orchestral.read(FakePage(symbolfont.AUGMENTATION_DOT), {"M": "music"})
    assert (
        orchestral.read(FakePage(symbolfont.AUGMENTATION_DOT), {"M": "extra"}).extra[0].code == symbolfont.AUGMENTATION_DOT
    )


@pytest.mark.parametrize(
    ("name", "written"),
    [("Clarinet I", 4), ("Horn III/IV", 3), ("Double Bass", 2), ("Flute I", 2), ("Harp", 2)],
)
def test_each_part_prints_the_concert_key_moved_by_its_transposition(name: str, written: int) -> None:
    assert orchestral.written_key(2, orchestral.transposition_of(name)) == written


class _BarExtras(TypedDict, total=False):
    key: int
    time: tuple[int, int]


class _NoteExtras(TypedDict, total=False):
    staff: int | None
    tuplet: bool
    ratio: Fraction
    dots: int


def bar(
    number: int,
    voices: list[list[Event]],
    voices2: list[list[Event]] | None = None,
    **kw: Unpack[_BarExtras],
) -> Bar:
    made = Bar(number, 0, 0.0, 100.0, Fraction(4), **kw)
    made.voices = voices
    made.voices2 = voices2 or []
    return made


def note(x: float, step: str, octave: int, length: Fraction, onset: Fraction, **kw: Unpack[_NoteExtras]) -> Event:
    return Event(x, [Head(x, 0.0, step, octave)], length, onset=onset, **kw)


def small_orchestra() -> Orchestra:
    """Build a clarinet with a quintuplet, and a harp whose 1 line crosses from its lower staff to its upper.

    Returns:
        The orchestra of the clarinet and the harp.

    """
    fives = [note(10.0 + i, "C", 5, Fraction(1, 4), Fraction(i, 5), tuplet=True, ratio=Fraction(4, 5)) for i in range(5)]
    clarinet = bar(1, [[*fives, note(30.0, "E", 5, Fraction(3), Fraction(1), dots=1)]], key=4, time=(4, 4))
    clarinet.clefs = [(1, Fraction(0), ("G", 2, 0))]
    crossing = [note(10.0 + i, "D", 3 + i // 8, Fraction(1, 2), Fraction(i, 2), staff=2 if i < 4 else 1) for i in range(8)]
    harp = bar(1, [crossing], [], key=2, time=(4, 4))
    harp.clefs = [(1, Fraction(0), ("G", 2, 0)), (2, Fraction(0), ("F", 4, 0)), (2, Fraction(2), ("G", 2, 0))]
    harp.texts2 = [Text(12.0, 0.0, [("p", False, True)], above=False, kind="dynamic")]
    return Orchestra(
        [
            Part("Clarinet I", "Cl. I", 1, orchestral.transposition_of("Clarinet I"), 4, [clarinet]),
            Part("Harp", "Hp.", 2, None, 2, [harp]),
        ],
        minor=False,
    )


def test_full_score_writes_every_part_with_its_transposition_staves_and_crossing_line(tmp_path: pathlib.Path) -> None:
    text = musicxml.write_orchestra(small_orchestra())
    root = ET.fromstring(text.split("\n", 1)[1])
    clarinet, harp = root.findall("part")
    shift = clarinet.find("measure/attributes/transpose")
    assert shift is not None
    assert (shift.findtext("diatonic"), shift.findtext("chromatic")) == ("-1", "-2")
    fifths = clarinet.find("measure/attributes/key/fifths")
    assert fifths is not None
    assert fifths.text == "4"
    assert {f"{m.findtext('actual-notes')}:{m.findtext('normal-notes')}" for m in clarinet.iter("time-modification")} == {
        "5:4",
    }
    assert harp.find("measure/attributes/transpose") is None
    assert harp.findtext("measure/attributes/staves") == "2"
    assert [(c.get("number"), c.findtext("sign")) for c in harp.iter("clef")] == [("1", "G"), ("2", "F"), ("2", "G")]
    crossing = [(n.findtext("voice"), n.findtext("staff")) for n in harp.iter("note")]
    assert {voice for voice, _ in crossing} == {"1"}
    assert {staff for _, staff in crossing} == {"1", "2"}
    assert harp.find("measure/direction/direction-type/dynamics/p") is not None
    assert harp.findtext("measure/direction/staff") == "2"
    path = tmp_path / "small.musicxml"
    path.write_text(text, encoding="utf-8")
    assert fullcheck.content_problems(small_orchestra(), path) == []


def test_a_duration_the_divisions_cannot_hold_is_refused_rather_than_cut() -> None:
    with pytest.raises(ValueError, match="divisions"):
        musicxml.note_xml(Event(0.0, [], Fraction(1, 4), tuplet=True, ratio=Fraction(4, 5)), 1, 48)


def test_a_full_score_song_names_3_files_and_its_key_from_its_strings(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = tmp_path / orchestral.FOLDER
    folder.mkdir()
    path = folder / "Score-D.pdf"
    path.write_bytes(b"")
    monkeypatch.setattr(song, "title_of", lambda _path: "Title")
    monkeypatch.setattr(orchestral, "decode", lambda _path: small_orchestra())
    with pytest.raises(song.SongRefusedError, match="Harp"):
        song.song_of(folder / "Score-E.pdf")
    found = song.song_of(path)
    assert found.orchestral
    assert found.files() == {found.score_file, found.original_pdf}
    with pytest.raises(song.SongRefusedError, match="full score"):
        song.decode(path)


def test_a_lead_sheet_folder_is_never_read_as_a_full_score(tmp_path: pathlib.Path) -> None:
    assert not song.is_orchestral(tmp_path / "Lead" / "Song-C.pdf")
    assert song.is_orchestral(tmp_path / orchestral.FOLDER / "Score-D.pdf")


def orchestral_song() -> song.Song:
    return song.Song("Score", "D", 2, "C", 0, "down", -2, "Title", orchestral=True)


def test_the_gate_gives_a_full_score_its_own_checks_and_none_of_the_lead_sheet_ones(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gate, "song_of", lambda _src, _score=None: orchestral_song())
    monkeypatch.setattr(gate, "decode", lambda _src: pytest.fail("a full score was decoded as a lead sheet"))
    for name in ("check", "chord_beat_problems"):
        monkeypatch.setattr(gate.verify, name, lambda *_a, **_k: pytest.fail("a lead-sheet check ran on a full score"))
    monkeypatch.setattr(gate, "piano_problems", lambda *_a: pytest.fail("the piano check ran on a full score"))
    ran = []

    def judged(
        _src: pathlib.Path,
        _out: pathlib.Path,
        _work: pathlib.Path,
        info: song.Song,
    ) -> tuple[list[str], list[str]]:
        ran.append(info.name)
        return [], []

    monkeypatch.setattr(fullcheck, "problems", judged)
    assert gate.check_song(tmp_path / "Score-D.pdf", tmp_path, tmp_path) == ([], [])
    assert ran == ["Score"]
    assert not {"chord beats", "signs built in", "piano", "lines", "even bars", "chord seat", "line start"} & set(
        fullcheck.CHECKS,
    )


def test_a_full_score_builds_its_3_files_and_never_lays_out_a_sheet(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = tmp_path / orchestral.FOLDER / "Score-D.pdf"
    src.parent.mkdir()
    src.write_bytes(b"source")
    monkeypatch.setenv("MUSE_PRIVATE", str(tmp_path / "private"))
    monkeypatch.setattr(build, "song_of", lambda _src: orchestral_song())
    monkeypatch.setattr(build.buildcache, "sheet_code", list)
    monkeypatch.setattr(build.layout, "lay", lambda *_a: pytest.fail("a full score went through the lead-sheet layout"))

    def made(_src: pathlib.Path, out_dir: pathlib.Path, _work: pathlib.Path, info: song.Song) -> dict[str, object]:
        for name in info.files():
            (out_dir / name).write_bytes(b"x")
        return {"pdf pages": 1}

    monkeypatch.setattr(fullscore, "build", made)
    report = build.song(src, tmp_path / "out", tmp_path / "work")
    assert report["built"] == ["original"]
    assert sorted(p.name for p in (tmp_path / "out" / "Score").iterdir()) == sorted(
        pathlib.Path(name).name for name in orchestral_song().files()
    )


def test_the_score_breaks_where_the_source_breaks(tmp_path: pathlib.Path) -> None:
    bars = [bar(n, [[Event(0.0, [], Fraction(4), bar_rest=True)]]) for n in range(1, 6)]
    bars[2].new_system = True
    bars[3].new_system = bars[3].new_page = True
    score = Orchestra([Part("Flute I", "Fl.", 1, None, 2, bars)], minor=False)
    source = tmp_path / "import.mscz"
    with zipfile.ZipFile(source, "w") as z:
        z.writestr(
            "score.mscx",
            "<museScore><Score><Style><Spatium>1.6</Spatium></Style><Staff>"
            + "<Measure><voice/></Measure>" * 5
            + "</Staff></Score></museScore>",
        )
    target = tmp_path / "final.mscz"
    port.edited(source, target, fullscore.SPATIUM, fullscore.DISTANCE, fullscore.breaks_of(score))
    with zipfile.ZipFile(target) as z:
        text = z.read("score.mscx").decode()
    measures = text.split("<Measure>")[1:]
    assert [re.findall(r"<subtype>(\w+)</subtype>", m) for m in measures] == [[], ["line"], ["page"], [], []]
    assert f"<Spatium>{fullscore.SPATIUM}</Spatium>" in text


def test_the_gate_refuses_systems_that_open_off_the_source_s_bars(tmp_path: pathlib.Path) -> None:
    bars = [bar(n, [[Event(0.0, [], Fraction(4), bar_rest=True)]]) for n in range(1, 5)]
    bars[2].new_system = True
    score = Orchestra([Part("Flute I", "Fl.", 1, None, 2, bars)], minor=False)
    mpos = tmp_path / "final.mpos"

    def laid(rows: list[int]) -> list[str]:
        mpos.write_text(
            "<score><elements>"
            + "".join(f'<element id="{n}" x="0" y="{y}" sx="1" sy="1" page="0"/>' for n, y in enumerate(rows))
            + "</elements></score>",
        )
        return fullscore.system_problems(score, mpos)

    assert laid([100, 100, 300, 300]) == []
    assert laid([100, 100, 100, 300]) == ["systems open on bars [1, 4], the source's on [1, 3]"]


def test_the_gate_finds_a_wrong_pitch_a_lost_bar_and_a_lost_transposition(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "score.musicxml"
    path.write_text(musicxml.write_orchestra(small_orchestra()), encoding="utf-8")
    wrong = small_orchestra()
    wrong.parts[0].bars[0].voices[0][-1].heads[0].step = "F"
    assert fullcheck.content_problems(wrong, path) == [
        "Clarinet I bar 1: pitches [72, 72, 72, 72, 72, 76] against [72, 72, 72, 72, 72, 77]",
    ]
    longer = small_orchestra()
    longer.parts[1].bars.append(bar(2, [[Event(0.0, [], Fraction(4), bar_rest=True)]]))
    assert fullcheck.content_problems(longer, path) == ["Harp: 1 bars against 2"]
    plain = small_orchestra()
    plain.parts[0].transposition = None
    assert fullcheck.content_problems(plain, path) == ["Clarinet I: transposition (-1, -2, 0) against None"]


def test_preflight_reads_a_full_score_with_its_own_reader(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / orchestral.FOLDER / "Score-D.pdf"
    path.parent.mkdir()
    path.write_bytes(b"source")
    monkeypatch.setattr(song, "title_of", lambda _path: "Title")
    monkeypatch.setattr(orchestral, "decode", lambda _path: small_orchestra())
    monkeypatch.setattr(preflight, "decode", lambda _src: pytest.fail("a full score was read as a lead sheet"))
    assert not preflight.song_problem(path)
    wrong = tmp_path / orchestral.FOLDER / "Score-E.pdf"
    wrong.write_bytes(b"source")
    assert preflight.song_problem(wrong).startswith("SongRefusedError")


def test_a_mark_between_2_systems_belongs_to_the_nearer_one_only() -> None:
    upper = orchestral.System(
        0,
        [orchestral.Staff(100.0, 140.0, 20.0, 560.0), orchestral.Staff(160.0, 200.0, 20.0, 560.0)],
        ["A", "B"],
        [],
    )
    lower = orchestral.System(0, [orchestral.Staff(260.0, 300.0, 20.0, 560.0)], ["C"], [])
    upper.below = lower.above = (200.0 + 260.0) / 2
    for y in (215.0, 225.0, 235.0, 245.0):
        assert upper.holds(y, 40.0, 30.0) != lower.holds(y, 40.0, 30.0)
