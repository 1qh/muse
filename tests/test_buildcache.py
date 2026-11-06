"""A cache must never hide a stale file."""
import pathlib
import subprocess
import types

from muse import build, buildcache, preflight


def setup(tmp_path):
    source, style, code, out = (tmp_path / n for n in ("s.pdf", "s.mss", "c.py", "o.pdf"))
    for path in (source, style, code, out):
        path.write_text("a")
    key = buildcache.key_of(source, "song", [code], style=style)
    return source, style, code, out, key, buildcache.record(key, [out])


def test_an_unchanged_build_is_fresh(tmp_path):
    source, style, code, out, key, entry = setup(tmp_path)
    assert buildcache.fresh(entry, buildcache.key_of(source, "song", [code], style=style), [out])


def test_changing_source_style_code_or_song_forces_a_rebuild(tmp_path):
    source, style, code, out, key, entry = setup(tmp_path)
    for path in (source, style, code):
        path.write_text("b")
        assert not buildcache.fresh(entry, buildcache.key_of(source, "song", [code], style=style), [out])
        path.write_text("a")
    assert not buildcache.fresh(entry, buildcache.key_of(source, "other", [code], style=style), [out])


def test_a_changed_or_missing_output_forces_a_rebuild(tmp_path):
    source, style, code, out, key, entry = setup(tmp_path)
    out.write_text("edited")
    assert not buildcache.fresh(entry, key, [out])
    out.unlink()
    assert not buildcache.fresh(entry, key, [out])


def test_piano_code_is_outside_the_other_sheets_key():
    assert "piano.py" not in {p.name for p in buildcache.code_files(skip=buildcache.PIANO_ONLY_CODE)}
    assert "piano.py" in {p.name for p in buildcache.code_files()}


def test_workers_follow_the_free_cores():
    assert build.free_workers(19, cores=10, load=55.0) == 1
    assert build.free_workers(19, cores=10, load=4.0) == 6
    assert build.free_workers(2, cores=10, load=0.0) == 2


def test_gate_code_is_outside_every_build_key():
    assert not buildcache.CHECK_ONLY_CODE & {p.name for p in buildcache.code_files(skip=buildcache.CHECK_ONLY_CODE)}


def test_a_failing_code_check_stops_the_rest():
    calls = []

    def check(command, **_):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, "boom", "")

    assert preflight.code_problems(check)[0].startswith("tests: boom")
    assert len(calls) == 1


def test_a_song_with_no_piano_part_or_a_refused_source_is_named(monkeypatch, tmp_path):
    source = tmp_path / "x.pdf"
    source.write_text("x")
    monkeypatch.setattr(preflight, "decode", lambda _: object())
    monkeypatch.setattr(preflight, "song_of", lambda *_: "song")
    monkeypatch.setattr(preflight.piano, "parts", lambda _: [])
    assert preflight.song_problem(source) == "no piano part resolved"
    monkeypatch.setattr(preflight, "decode", lambda _: (_ for _ in ()).throw(ValueError("bad font")))
    assert "bad font" in preflight.song_problem(source)


def test_build_key_covers_the_extension() -> None:
    names = {p.name for p in buildcache.code_files()}
    assert {"main.js", "manifest.json"} <= names


def test_songs_build_in_the_owners_priority_order_then_by_name(tmp_path, monkeypatch) -> None:
    (tmp_path / "PRIORITY.txt").write_text("Zeta Beta\n", encoding="utf-8")
    monkeypatch.setenv("MUSE_PRIVATE", str(tmp_path))
    monkeypatch.setattr(build, "song_of", lambda src: types.SimpleNamespace(name=src.stem.split("-")[0]))
    sources = [pathlib.Path(f"{name}-Em.pdf") for name in ("Alpha", "Beta", "Gamma", "Zeta")]
    assert [src.stem.split("-")[0] for src in build.ordered(sources)] == ["Zeta", "Beta", "Alpha", "Gamma"]


def test_a_build_gives_each_song_worker_its_share_of_launch_slots(monkeypatch) -> None:
    from muse import launch

    monkeypatch.setenv(launch.SLOTS_VARIABLE, "3")
    assert launch.slots() == 3
    monkeypatch.delenv(launch.SLOTS_VARIABLE)
    assert launch.slots() >= 1


def test_a_print_mode_build_is_never_reused_as_strict(tmp_path, monkeypatch) -> None:
    from muse import mode

    source = tmp_path / "Song-Em.pdf"
    source.write_bytes(b"pdf")
    monkeypatch.setattr(build, "song_of", lambda src: types.SimpleNamespace(name="Song"))
    monkeypatch.setattr(mode, "PRINT", False)
    strict = build.variant_key(source, "original")
    monkeypatch.setattr(mode, "PRINT", True)
    assert build.variant_key(source, "original") != strict
