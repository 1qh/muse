"""Per-song build and gate state: what each output was built from, and the layout decisions behind it."""
from __future__ import annotations

import ast
import hashlib
import json
import os
import pathlib
import shutil
from typing import Any

ROOT = pathlib.Path(__file__).parent
STYLE = ROOT / "style.mss"
SHEET_WRITERS = ("layout", "port", "piano", "chordnames", "scorefile")  # the modules a song's build writes its sheets through; every module they import can shape a sheet, and the orchestrating build module, which imports the checks too, is not one
STATE_DIR = "build-state"


def file_digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


CODE_SUFFIXES = (".py", ".js", ".json")


def code_files(root: pathlib.Path = ROOT, skip: frozenset[str] | set[str] = frozenset()) -> list[pathlib.Path]:
    """Every file of the package that can shape an output: its Python and the MuseScore extension it installs."""
    return sorted(path for path in root.rglob("*") if path.is_file() and path.suffix in CODE_SUFFIXES and "__pycache__" not in path.parts and path.name not in skip)


def imports_of(module: str, root: pathlib.Path = ROOT) -> set[str]:
    """The package's own modules 1 module imports, read from its syntax tree."""
    tree = ast.parse((root / f"{module}.py").read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "muse":
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("muse."):
            found.add(node.module.split(".")[1])
        elif isinstance(node, ast.Import):
            found |= {alias.name.split(".")[1] for alias in node.names if alias.name.startswith("muse.")}
    return {name for name in found if (root / f"{name}.py").exists()}


def sheet_code(root: pathlib.Path = ROOT) -> list[pathlib.Path]:
    """Every file that can shape a sheet: the import closure of the modules that write one, and the MuseScore extension they install."""
    seen: set[str] = set()
    todo = list(SHEET_WRITERS)
    while todo:
        module = todo.pop()
        if module not in seen:
            seen.add(module)
            todo += imports_of(module, root)
    return sorted([root / f"{module}.py" for module in seen] + [path for path in root.rglob("*") if path.is_file() and path.suffix in set(CODE_SUFFIXES) - {".py"} and "__pycache__" not in path.parts])


def tool_identity() -> str:
    """Which MuseScore runs, by size and time, without paying to launch it."""
    parts = []
    for name in ("mscore", os.environ.get("MUSE_MS3") or ""):
        found = shutil.which(name) if name else None
        if found:
            stat = pathlib.Path(found).resolve().stat()
            parts.append(f"{found}:{stat.st_size}:{int(stat.st_mtime)}")
    return "|".join(parts)


def key_of(source: pathlib.Path, song: str, files: list[pathlib.Path], extra: str = "", style: pathlib.Path = STYLE) -> str:
    """Digest of everything an output depends on: source bytes, song settings, code, style, tools."""
    whole = hashlib.sha256()
    for part in (file_digest(source), song, file_digest(style), tool_identity(), extra, *(f"{path.name}={file_digest(path)}" for path in files)):
        whole.update(part.encode("utf-8"))
        whole.update(b"\0")
    return whole.hexdigest()


def state_path(song: str) -> pathlib.Path:
    return pathlib.Path(os.environ["MUSE_PRIVATE"]) / STATE_DIR / f"{song}.json"


def load(song: str) -> dict[str, dict[str, Any]]:
    path = state_path(song)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save(song: str, state: dict[str, dict[str, Any]]) -> None:
    path = state_path(song)
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = path.with_suffix(".tmp")
    fresh.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    fresh.replace(path)


def fresh(entry: dict[str, Any] | None, key: str, outputs: list[pathlib.Path]) -> bool:
    """True only when the inputs match and every output still holds the bytes that were built."""
    if not entry or entry.get("key") != key:
        return False
    recorded = entry.get("outputs", {})
    if set(recorded) != {str(path) for path in outputs}:
        return False
    return all(path.exists() and file_digest(path) == recorded[str(path)] for path in outputs)


def record(key: str, outputs: list[pathlib.Path], **rest: object) -> dict[str, Any]:
    return {"key": key, "outputs": {str(path): file_digest(path) for path in outputs}, **rest}
