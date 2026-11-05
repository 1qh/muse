"""Published state per song, read from the sheets repo's HEAD and the build records, never recalled."""
from __future__ import annotations

import hashlib
import pathlib
import subprocess
import sys

from muse import buildcache, port
from muse.song import song_of, sources


def blob(out: pathlib.Path, name: str) -> bytes | None:
    done = subprocess.run(["git", "-C", str(out), "show", f"HEAD:{name}"], capture_output=True, check=False)
    return done.stdout if done.returncode == 0 else None


def main() -> None:
    src_dirs, out = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
    rules = port.layout_rules()
    current = stale = missing = 0
    for src in sources(src_dirs):
        info = song_of(src)
        state = buildcache.load(info.name)
        verdicts = []
        for variant, entry in sorted(state.items()):
            decision = entry.get("decision") or {}
            recorded = entry.get("outputs", {})
            pushed = all((data := blob(out, "/".join(pathlib.Path(path).parts[-2:]))) is not None and hashlib.sha256(data).hexdigest() == digest for path, digest in recorded.items())
            ok = decision.get("rules") == rules and pushed
            verdicts.append(f"{variant}:{'current' if ok else 'stale'}:{decision.get('spatium')}x{decision.get('pages')}p")
            current += ok
            stale += not ok
        if not state:
            missing += 1
        print(info.name.ljust(22), " ".join(verdicts) or "never built")
    print(f"sheets current {current}, stale {stale}, songs never built {missing}")


if __name__ == "__main__":
    main()
