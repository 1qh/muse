"""A cache must never hide a stale file."""
from muse import buildcache, port


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
    assert port.free_workers(19, cores=10, load=55.0) == 1
    assert port.free_workers(19, cores=10, load=4.0) == 6
    assert port.free_workers(2, cores=10, load=0.0) == 2
