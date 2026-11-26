"""The only way sheets reach their repo: pass the gate, push, then purge replaced binaries."""

from __future__ import annotations

import pathlib
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from typing import NamedTuple

from muse import gate, toolpath

PUSH_TRIES = 4
SH_TIMEOUT_SECONDS = 3000
ERROR_TAIL_CHARS = 600
PUSH_WAIT = 60.0
Runner = Callable[..., str]
Judge = Callable[[pathlib.Path, pathlib.Path, pathlib.Path, tuple[str, ...]], dict[str, list[str]]]


class RefusedError(RuntimeError):
    """Publishing stopped before anything left this machine."""


def sh(*args: str, cwd: pathlib.Path | None = None) -> str:
    done = subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=SH_TIMEOUT_SECONDS,
        check=False,
    )
    if done.returncode:
        head = " ".join(args[:3])
        msg = f"{head}: {done.stdout[-ERROR_TAIL_CHARS:]}{done.stderr[-ERROR_TAIL_CHARS:]}"
        raise RefusedError(msg)
    return done.stdout.strip()


def stray_files(tracked: str) -> list[str]:
    """List anything tracked in the sheets repo but a PDF or a MuseScore file.

    Returns:
        The tracked names that are neither a PDF nor a MuseScore file.

    """
    return [name for name in tracked.split("\n") if name and not name.endswith((".pdf", ".mscz"))]


def pushed(
    run: Runner,
    command: tuple[str, ...],
    where: pathlib.Path,
    tries: int = PUSH_TRIES,
    wait: float = PUSH_WAIT,
) -> None:
    """Retry past a forge's server error; any other refusal, or the last try, stands.

    Raises:
        RefusedError: A refusal that is not a server error, or the last try's refusal.

    """
    for attempt in range(1, tries + 1):
        try:
            run(*command, cwd=where)
        except RefusedError as error:
            if attempt == tries or "Internal Server Error" not in str(error):
                raise
            time.sleep(wait)
        else:
            return


def keep_last_good(out: pathlib.Path, song: str, run: Runner) -> None:
    """Put a failing song's folder back to what is published; a song never published leaves nothing."""
    if run("git", "ls-files", "--", song, cwd=out):
        run("git", "reset", "-q", "HEAD", "--", song, cwd=out)
        run("git", "checkout", "HEAD", "--", song, cwd=out)
    run("git", "clean", "-fdq", "--", song, cwd=out)


SHEET_SUFFIXES = (".pdf", ".mscz")
SHOWN_FAILURES = 3


def superseded(current_tree: str, history: str) -> list[str]:
    """Find blob ids of sheet files in history that the current sheets do not hold.

    These are the only versions a purge removes, so each commit keeps its current files.

    Returns:
        The sorted stale blob ids.

    """
    current = {line.split()[2] for line in current_tree.splitlines() if line.split()[1:2] == ["blob"]}
    stale = set()
    for line in history.splitlines():
        blob, _, path = line.partition(" ")
        if path.endswith(SHEET_SUFFIXES) and blob not in current:
            stale.add(blob)
    return sorted(stale)


def resync(out: pathlib.Path, run: Runner = sh) -> None:
    """Reset the local sheets repo to its remote.

    A build stopped mid-publish can leave it behind a history another publish rewrote.
    """
    run("git", "fetch", "--quiet", "origin", cwd=out)
    run("git", "reset", "--quiet", "--hard", "@{u}", cwd=out)


def commit_message(songs: list[str]) -> str:
    """Name what a publish brings, since the history purge leaves no earlier file to diff against.

    Returns:
        The commit message for the published songs.

    """
    return f"feat: publish {', '.join(songs)}" if songs else "chore: refresh sheets"


class Places(NamedTuple):
    """Where a publish reads its sources, keeps its sheets repo and does its work."""

    src: pathlib.Path
    out: pathlib.Path
    work: pathlib.Path


def publish(
    places: Places,
    run: Runner = sh,
    judge: Judge = gate.verdicts,
    names: tuple[str, ...] = (),
    *,
    purging: bool = True,
) -> dict[str, list[str]]:
    """Publish every song passing its gate; returns the blocked songs, each keeping its last good folder, with reasons.

    With `purging` false the superseded versions stay in history until `purge` runs, once for a whole build.

    Returns:
        The blocked songs mapped to their failure reasons.

    Raises:
        SystemExit: The sheets repo holds a file that is neither a PDF nor a MuseScore file.

    """
    src, out, work = places
    verdicts = judge(src, out, work, names)
    blocked = {song: failures for song, failures in verdicts.items() if failures}
    for song in blocked:
        keep_last_good(out, song, run)
    for song in verdicts:
        if song not in blocked:
            run("git", "add", "-A", "--", song, cwd=out)
    extras = stray_files(run("git", "ls-files", cwd=out))
    if extras:
        msg = f"the sheets repo holds only PDF and MuseScore files, refusing: {extras}"
        raise SystemExit(msg)
    if run("git", "diff", "--cached", "--name-only", cwd=out):
        run("git", "commit", "-m", commit_message(sorted(song for song in verdicts if song not in blocked)), cwd=out)
    elif not run("git", "rev-list", "@{u}..HEAD", cwd=out):
        return blocked
    pushed(run, ("git", "push"), out)
    if purging:
        purge(out, run)
    return blocked


def purge(out: pathlib.Path, run: Runner = sh) -> None:
    """Strip every superseded sheet version from the sheets repo's history, keeping its current files.

    Raises:
        RefusedError: The purge changed the current files, or the local tree differs after it.

    """
    tree = run("git", "rev-parse", "HEAD:", cwd=out)
    url = run("git", "remote", "get-url", "origin", cwd=out)
    with tempfile.TemporaryDirectory() as tmp:
        mirror = pathlib.Path(tmp) / "mirror.git"
        run("git", "clone", "--mirror", url, str(mirror))
        stale = superseded(
            run("git", "ls-tree", "-r", "HEAD", cwd=mirror),
            run("git", "rev-list", "--objects", "--all", cwd=mirror),
        )
        if stale:
            listed = pathlib.Path(tmp) / "superseded.txt"
            listed.write_text("\n".join(stale) + "\n", encoding="utf-8")
            run("git", "filter-repo", "--strip-blobs-with-ids", str(listed), "--force", cwd=mirror)
        run("git", "reflog", "expire", "--expire=now", "--all", cwd=mirror)
        run("git", "gc", "--prune=now", "--aggressive", cwd=mirror)
        if run("git", "rev-parse", "HEAD:", cwd=mirror) != tree:
            msg = "purge changed the current files; nothing force-pushed"
            raise RefusedError(msg)
        pushed(run, ("git", "push", "--force", url, "refs/heads/*:refs/heads/*"), mirror)
    run("git", "fetch", "origin", cwd=out)
    run("git", "reset", "--hard", "origin/HEAD", cwd=out)
    run("git", "reflog", "expire", "--expire=now", "--all", cwd=out)
    run("git", "gc", "--prune=now", cwd=out)
    if run("git", "rev-parse", "HEAD:", cwd=out) != tree:
        msg = "local tree differs after the purge"
        raise RefusedError(msg)


def main() -> None:
    if toolpath.which("git-filter-repo") is None:
        msg = "git-filter-repo not found"
        raise SystemExit(msg)
    src, out, work = map(pathlib.Path, sys.argv[1:4])
    try:
        blocked = publish(Places(src, out, work), names=tuple(sys.argv[4:]))
    except RefusedError as error:
        raise SystemExit(str(error)) from error
    for song, failures in blocked.items():
        sys.stdout.write(f"blocked {song}: {failures[:SHOWN_FAILURES]}" + "\n")
    sys.stdout.write(("ok" if not blocked else f"blocked {len(blocked)}") + "\n")
    raise SystemExit(1 if blocked else 0)


if __name__ == "__main__":
    main()
