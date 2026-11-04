"""The only way sheets reach their repo: confirm it is private, pass the gate, push, then purge replaced binaries."""
from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable

from muse import gate

Runner = Callable[..., str]
Judge = Callable[[pathlib.Path, pathlib.Path, pathlib.Path, tuple[str, ...]], dict[str, list[str]]]


class Refused(RuntimeError):
    """Publishing stopped before anything left this machine."""


def sh(*args: str, cwd: pathlib.Path | None = None) -> str:
    done = subprocess.run(args, cwd=cwd, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=3000)
    if done.returncode:
        raise Refused(f"{' '.join(args[:3])}: {done.stdout[-600:]}{done.stderr[-600:]}")
    return done.stdout.strip()


def visibility(out: pathlib.Path, run: Runner) -> str:
    return run("gh", "repo", "view", "--json", "visibility", "-q", ".visibility", cwd=out)


def stray_files(tracked: str) -> list[str]:
    """Anything tracked in the sheets repo but a PDF or a MuseScore file."""
    return [name for name in tracked.split("\n") if name and not name.endswith((".pdf", ".mscz"))]


def keep_last_good(out: pathlib.Path, song: str, run: Runner) -> None:
    """Put a failing song's folder back to what is published; a song never published leaves nothing."""
    if run("git", "ls-files", "--", song, cwd=out):
        run("git", "reset", "-q", "HEAD", "--", song, cwd=out)
        run("git", "checkout", "HEAD", "--", song, cwd=out)
    run("git", "clean", "-fdq", "--", song, cwd=out)


def publish(src: pathlib.Path, out: pathlib.Path, work: pathlib.Path, run: Runner = sh, judge: Judge = gate.verdicts, names: tuple[str, ...] = ()) -> dict[str, list[str]]:
    """Publish every song passing its gate; returns the blocked songs, each keeping its last good folder, with reasons."""
    if visibility(out, run) != "PRIVATE":
        raise Refused(f"{out} is not private; sheets may only go to a private repo")
    verdicts = judge(src, out, work, names)
    blocked = {song: failures for song, failures in verdicts.items() if failures}
    for song in blocked:
        keep_last_good(out, song, run)
    for song in verdicts:
        if song not in blocked:
            run("git", "add", "-A", "--", song, cwd=out)
    extras = stray_files(run("git", "ls-files", cwd=out))
    if extras:
        raise SystemExit(f"the sheets repo holds only PDF and MuseScore files, refusing: {extras}")
    if not run("git", "diff", "--cached", "--name-only", cwd=out):
        return blocked
    run("git", "commit", "-m", "fix: re-export sheets", cwd=out)
    run("git", "push", cwd=out)
    tree = run("git", "rev-parse", "HEAD^{tree}", cwd=out)
    url = run("git", "remote", "get-url", "origin", cwd=out)
    with tempfile.TemporaryDirectory() as tmp:
        mirror = pathlib.Path(tmp) / "mirror.git"
        run("git", "clone", "--mirror", url, str(mirror))
        run("bfg", "--delete-files", "*.{pdf,mscz}", ".", cwd=mirror)
        run("git", "reflog", "expire", "--expire=now", "--all", cwd=mirror)
        run("git", "gc", "--prune=now", "--aggressive", cwd=mirror)
        if run("git", "rev-parse", "HEAD^{tree}", cwd=mirror) != tree:
            raise Refused("purge changed the current files; nothing force-pushed")
        run("git", "push", "--force", cwd=mirror)
    run("git", "fetch", "origin", cwd=out)
    run("git", "reset", "--hard", "origin/HEAD", cwd=out)
    run("git", "reflog", "expire", "--expire=now", "--all", cwd=out)
    run("git", "gc", "--prune=now", cwd=out)
    if run("git", "rev-parse", "HEAD^{tree}", cwd=out) != tree:
        raise Refused("local tree differs after the purge")
    if visibility(out, run) != "PRIVATE":
        raise Refused(f"{out} stopped being private")
    return blocked


def main() -> None:
    if not shutil.which("bfg"):
        raise SystemExit("bfg not found")
    src, out, work = map(pathlib.Path, sys.argv[1:4])
    try:
        blocked = publish(src, out, work, names=tuple(sys.argv[4:]))
    except Refused as error:
        raise SystemExit(str(error)) from error
    for song, failures in blocked.items():
        print(f"blocked {song}: {failures[:3]}")
    print("ok" if not blocked else f"blocked {len(blocked)}")
    raise SystemExit(1 if blocked else 0)


if __name__ == "__main__":
    main()
