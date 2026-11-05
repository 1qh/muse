"""Everything that fails in seconds, before any build: code checks stop the chain, a song's own failure stops that song."""
from __future__ import annotations

import pathlib
import subprocess
import sys
from collections.abc import Callable

from muse import buildcache, oracle, piano
from muse.song import decode, song_of, sources

CODE_CHECKS = (
    ("tests", (sys.executable, "-m", "pytest", "-q", "-x")),
    ("lint", (sys.executable, "-m", "ruff", "check", "-q", "src", "tests")),
    ("types", (sys.executable, "-m", "mypy", "src")),
    ("control anchors", (sys.executable, "tests/mutate.py", "--anchors")),
)


def song_problem(src: pathlib.Path) -> str:
    """Why a source cannot be built, or an empty string: decode, song key, tone, piano parts."""
    try:
        score = decode(src)
        info = song_of(src, score)
        buildcache.key_of(src, repr(info), buildcache.code_files())
        if not piano.parts(score):
            return "no piano part resolved"
        lost = oracle.problems(src, score, info.title)
        if lost:
            return lost[0]
    except Exception as error:  # every refusal names its song and the others go on
        return f"{type(error).__name__}: {str(error)[:200]}"
    return ""


def code_problems(check: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> list[str]:
    """The first failing code check stops the rest."""
    for label, command in CODE_CHECKS:
        done = check(command, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        if done.returncode:
            return [f"{label}: {(done.stdout + done.stderr)[-600:]}"]
    return []


def main() -> None:
    bad = {src.stem: found for src in sources(pathlib.Path(sys.argv[1])) if (found := song_problem(src))}
    for song, reason in bad.items():
        print(f"blocked {song}: {reason}")
    code = code_problems()
    for problem in code:
        print(f"code: {problem}")
    print("ok" if not bad and not code else f"code failures {len(code)}, songs blocked {len(bad)}")
    raise SystemExit(2 if code else 1 if bad else 0)


if __name__ == "__main__":
    main()
