"""The only way sheets reach their repo: confirm it is private, pass the gate, push, then purge replaced binaries."""
from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable

Runner = Callable[..., str]


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


def publish(src: pathlib.Path, out: pathlib.Path, work: pathlib.Path, run: Runner = sh) -> None:
    if visibility(out, run) != "PRIVATE":
        raise Refused(f"{out} is not private; sheets may only go to a private repo")
    try:
        run(sys.executable, "-m", "muse.gate", str(src), str(out), str(work))
    except Refused as error:
        raise Refused(f"gate failed, nothing published:\n{error}") from error
    files = sorted(p.name for p in out.iterdir() if p.suffix in (".pdf", ".mscz"))
    run("git", "add", *files, cwd=out)
    extras = stray_files(run("git", "ls-files", cwd=out))
    if extras:
        raise SystemExit(f"the sheets repo holds only PDF and MuseScore files, refusing: {extras}")
    if run("git", "status", "--porcelain", cwd=out):
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
    print("ok")


def main() -> None:
    if not shutil.which("bfg"):
        raise SystemExit("bfg not found")
    src, out, work = map(pathlib.Path, sys.argv[1:4])
    try:
        publish(src, out, work)
    except Refused as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
