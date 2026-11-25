"""Per-song build and gate state: what each output was built from, and the layout decisions behind it."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import pathlib
from typing import NotRequired, TypedDict

from muse import launch


class Decision(TypedDict):
    spatium: float
    distance: float
    pages: NotRequired[int]
    lines: NotRequired[list[int]]


class CacheRecord(TypedDict):
    key: str
    outputs: dict[str, str]
    decision: NotRequired[Decision]
    notes: NotRequired[list[str]]


ROOT = pathlib.Path(__file__).parent
STYLE = ROOT / "style.mss"
SHEET_WRITERS = ("layout", "port", "piano", "chordnames", "scorefile", "fullscore")
STATE_DIR = "build-state"


def file_digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


CODE_SUFFIXES = (".py", ".js", ".json")


def code_files(root: pathlib.Path = ROOT, skip: frozenset[str] | set[str] = frozenset()) -> list[pathlib.Path]:
    """Every file of the package that can shape an output: its Python and the MuseScore extension it installs.

    Returns:
        The sorted code file paths.

    """
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix in CODE_SUFFIXES and "__pycache__" not in path.parts and path.name not in skip
    )


def imports_of(module: str, root: pathlib.Path = ROOT) -> set[str]:
    """Read the package's own modules 1 module imports from its syntax tree.

    Returns:
        The names of the package modules imported.

    """
    tree = ast.parse((root / f"{module}.py").read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "muse":
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module is not None and node.module.startswith("muse."):
            found.add(node.module.split(".")[1])
        elif isinstance(node, ast.Import):
            found |= {alias.name.split(".")[1] for alias in node.names if alias.name.startswith("muse.")}
    return {name for name in found if (root / f"{name}.py").exists()}


def sheet_code(root: pathlib.Path = ROOT) -> list[pathlib.Path]:
    """Every file that can shape a sheet.

    That is the import closure of the modules that write one, and the MuseScore extension they install.

    Returns:
        The sorted file paths.

    """
    seen: set[str] = set()
    todo = list(SHEET_WRITERS)
    while todo:
        module = todo.pop()
        if module not in seen:
            seen.add(module)
            todo += imports_of(module, root)
    return sorted(
        [root / f"{module}.py" for module in seen]
        + [
            path
            for path in root.rglob("*")
            if path.is_file() and path.suffix in set(CODE_SUFFIXES) - {".py"} and "__pycache__" not in path.parts
        ],
    )


def tool_identity() -> str:
    """Which MuseScore runs, by size and time, without paying to launch it.

    Returns:
        The identity of every MuseScore found, joined by bars.

    """
    found = launch.mscore_command()
    stat = pathlib.Path(found).resolve().stat()
    return f"{found}:{stat.st_size}:{int(stat.st_mtime)}"


def key_of(
    source: pathlib.Path,
    song: str,
    files: list[pathlib.Path],
    extra: str = "",
    style: pathlib.Path = STYLE,
) -> str:
    """Digest of everything an output depends on: source bytes, song settings, code, style, tools.

    Returns:
        The hex digest.

    """
    whole = hashlib.sha256()
    for part in (
        file_digest(source),
        song,
        file_digest(style),
        tool_identity(),
        extra,
        *(f"{path.name}={file_digest(path)}" for path in files),
    ):
        whole.update(part.encode("utf-8"))
        whole.update(b"\0")
    return whole.hexdigest()


def state_path(song: str) -> pathlib.Path:
    return pathlib.Path(os.environ["MUSE_PRIVATE"]) / STATE_DIR / f"{song}.json"


def load(song: str) -> dict[str, CacheRecord]:
    path = state_path(song)
    if not path.exists():
        return {}
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        msg = f"{path}: cache is not an object"
        raise TypeError(msg)
    state: dict[str, CacheRecord] = {}
    for variant, raw in loaded.items():
        if not isinstance(variant, str):
            msg_0 = f"{path}: cache record has the wrong shape"
            raise TypeError(msg_0)
        state[variant] = _load_record(path, raw)
    return state


def _load_record(path: pathlib.Path, raw: object) -> CacheRecord:
    if not isinstance(raw, dict):
        msg = f"{path}: cache record has the wrong shape"
        raise TypeError(msg)
    if "key" not in raw or "outputs" not in raw:
        msg = f"{path}: cache record is missing key or outputs"
        raise KeyError(msg)
    key, outputs = raw["key"], raw["outputs"]
    if not isinstance(key, str) or not isinstance(outputs, dict):
        msg = f"{path}: cache record has the wrong shape"
        raise TypeError(msg)
    if any(not isinstance(name, str) or not isinstance(digest, str) for name, digest in outputs.items()):
        msg = f"{path}: cache outputs have the wrong shape"
        raise TypeError(msg)
    entry: CacheRecord = {"key": key, "outputs": outputs}
    if "decision" in raw:
        entry["decision"] = _load_decision(path, raw["decision"])
    if "notes" in raw:
        entry["notes"] = _load_notes(path, raw["notes"])
    return entry


def _load_decision(path: pathlib.Path, raw: object) -> Decision:
    if not isinstance(raw, dict):
        msg = f"{path}: cache decision has the wrong shape"
        raise TypeError(msg)
    if "spatium" not in raw or "distance" not in raw:
        msg = f"{path}: cache decision is missing spatium or distance"
        raise KeyError(msg)
    if not isinstance(raw["spatium"], (int, float)) or not isinstance(raw["distance"], (int, float)):
        msg = f"{path}: cache decision has the wrong shape"
        raise TypeError(msg)
    decision: Decision = {"spatium": raw["spatium"], "distance": raw["distance"]}
    if "pages" in raw:
        if not isinstance(raw["pages"], int):
            msg = f"{path}: cache decision has the wrong shape"
            raise TypeError(msg)
        decision["pages"] = raw["pages"]
    if "lines" in raw:
        if not isinstance(raw["lines"], list):
            msg = f"{path}: cache decision has the wrong shape"
            raise TypeError(msg)
        lines: list[int] = []
        for line in raw["lines"]:
            if not isinstance(line, int):
                msg = f"{path}: cache decision has the wrong shape"
                raise TypeError(msg)
            lines.append(line)
        decision["lines"] = lines
    return decision


def _load_notes(path: pathlib.Path, raw: object) -> list[str]:
    if not isinstance(raw, list):
        msg = f"{path}: cache notes have the wrong shape"
        raise TypeError(msg)
    notes: list[str] = []
    for note in raw:
        if not isinstance(note, str):
            msg = f"{path}: cache notes have the wrong shape"
            raise TypeError(msg)
        notes.append(note)
    return notes


def save(song: str, state: dict[str, CacheRecord]) -> None:
    path = state_path(song)
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = path.with_suffix(".tmp")
    fresh.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    fresh.replace(path)


def fresh(entry: CacheRecord | None, key: str, outputs: list[pathlib.Path], root: pathlib.Path | None = None) -> bool:
    """Report true only when the inputs match and every output still holds the bytes that were built.

    With `root`, an output under it is named relative to it, so the same bytes under another root still match.

    Returns:
        Whether the recorded outputs are still fresh.

    """
    if entry is None or entry["key"] != key:
        return False
    recorded = entry["outputs"]
    if set(recorded) != {_label(path, root) for path in outputs}:
        return False
    return all(path.exists() and file_digest(path) == recorded[_label(path, root)] for path in outputs)


def _label(path: pathlib.Path, root: pathlib.Path | None) -> str:
    return str(path.relative_to(root)) if root is not None and path.is_relative_to(root) else str(path)


def record(
    key: str,
    outputs: list[pathlib.Path],
    *,
    decision: Decision | None = None,
    notes: list[str] | None = None,
    root: pathlib.Path | None = None,
) -> CacheRecord:
    entry: CacheRecord = {"key": key, "outputs": {_label(path, root): file_digest(path) for path in outputs}}
    if decision is not None:
        entry["decision"] = decision
    if notes is not None:
        entry["notes"] = notes
    return entry
