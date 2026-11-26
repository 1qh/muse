"""Published state per song, read from the sheets repo's HEAD and the build records, never recalled."""

from __future__ import annotations

import hashlib
import pathlib
import subprocess
import sys

from muse import build, buildcache, gate, toolpath
from muse.song import song_of, sources

SONG_COLUMN = 22


def favourite_size(name: str) -> tuple[str, float]:
    """Read the favourite sheet's chosen variant and size from its status records.

    Returns:
        The tone variant when recorded, otherwise the original, and its chosen size.

    Raises:
        ValueError: the favourite variant has no recorded layout decision.

    """
    state = buildcache.load(name)
    variant = "tone" if "tone" in state else "original"
    entry = state.get(variant)
    decision = None if entry is None else entry.get("decision")
    if decision is None:
        message = f"{name}: no chosen size for {variant}"
        raise ValueError(message)
    return variant, decision["spatium"]


def blob(out: pathlib.Path, name: str) -> bytes | None:
    git = toolpath.which("git")
    if git is None:
        raise FileNotFoundError(2, "No such file or directory", "git")
    done = subprocess.run([git, "-C", str(out), "show", f"HEAD:{name}"], capture_output=True, check=False)
    return done.stdout if done.returncode == 0 else None


def main() -> None:
    src_dirs, out = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
    current = stale = missing = 0
    for src in sources(src_dirs):
        info = song_of(src)
        state = buildcache.load(info.name)
        verdicts = []
        for variant, entry in sorted(state.items()):
            decision = entry.get("decision")
            recorded = entry["outputs"]
            delivered = {path: digest for path, digest in recorded.items() if pathlib.Path(path).parent.name == info.name}
            pushed = bool(delivered) and all(
                (data := blob(out, "/".join(pathlib.Path(path).parts[-2:]))) is not None
                and hashlib.sha256(data).hexdigest() == digest
                for path, digest in delivered.items()
            )
            wanted = gate.gate_key(src) if variant == "gate" else build.variant_key(src, variant)
            ok = entry.get("key") == wanted and pushed
            size = f":{decision['spatium']}x{decision['pages']}p" if decision is not None and "pages" in decision else ""
            verdicts.append(f"{variant}:{'current' if ok else 'stale'}{size}")
            current += ok
            stale += not ok
        if not state:
            missing += 1
        sys.stdout.write(info.name.ljust(SONG_COLUMN) + " " + (" ".join(verdicts) or "never built") + "\n")
    sys.stdout.write(f"sheets current {current}, stale {stale}, songs never built {missing}\n")


if __name__ == "__main__":
    main()
