"""Entry points set terminal policy and prove tools before doing work."""

from __future__ import annotations

import ast
import importlib
import inspect
import pathlib
import signal
import subprocess
import sys

import pytest

from muse import build, gate, launch, publish, status, toolpath, verify

ENTRY_NAMES = (
    "build",
    "gate",
    "inverse",
    "navigate",
    "pdfcheck",
    "practice",
    "preflight",
    "publish",
    "status",
    "verify",
)
ENTRY_TEST_TIMEOUT = 5.0
ENTRY_FAILURE_CODE = 7
ENTRY_ERROR_TEXT = "unique stderr refusal: café"
ENTRY_BINARY = b"\x00\xffPDF\r\n"
ENTRY_SUCCESS_CODE = 0
ENTRY_LAST_TOOL = -1


class TerminalSetupCompleteError(Exception):
    """Stop a main immediately after its real terminal setup."""


def test_entry_inventory_covers_every_main() -> None:
    """Include every source module that defines a main function."""
    folder = pathlib.Path(launch.__file__).parent
    found = {
        path.stem
        for path in folder.glob("*.py")
        if any(
            isinstance(node, ast.FunctionDef) and node.name == "main"
            for node in ast.parse(path.read_text(encoding="utf-8")).body
        )
    }
    assert found == set(ENTRY_NAMES)


@pytest.mark.parametrize("name", ENTRY_NAMES)
def test_every_main_first_ignores_both_terminal_stops(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run each main through its real terminal setup and stop before any work."""
    module = importlib.import_module(f"muse.{name}")
    tree = ast.parse(inspect.getsource(module.main))
    definition = next(node for node in tree.body if isinstance(node, ast.FunctionDef))
    first = next(
        node for node in definition.body if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))
    )
    assert ast.unparse(first) == "launch.ignore_terminal_stops()"
    stops = (signal.SIGTTOU, signal.SIGTTIN)
    saved = {stop: signal.getsignal(stop) for stop in stops}
    setup = launch.ignore_terminal_stops

    def stop_after_setup() -> None:
        setup()
        raise TerminalSetupCompleteError

    monkeypatch.setattr(launch, "ignore_terminal_stops", stop_after_setup)
    try:
        for stop in stops:
            signal.signal(stop, signal.SIG_DFL)
        with pytest.raises(TerminalSetupCompleteError):
            module.main()
        assert all(signal.getsignal(stop) == signal.SIG_IGN for stop in stops)
    finally:
        for stop, handler in saved.items():
            signal.signal(stop, handler)


@pytest.mark.parametrize(
    ("name", "arguments", "tools"),
    [
        ("build", (), ("mscore",)),
        ("build", ("--publish",), ("mscore", "git", "git-filter-repo", "homr", "pdftotext")),
        ("build", (build.CHILD_FLAG, "source", "stage", "work"), ("mscore",)),
        ("build", (build.CHILD_FLAG, "source", "stage", "work", "sources"), ("mscore", "homr", "pdftotext")),
        ("gate", (), ("mscore", "homr", "pdftotext")),
        ("inverse", (), ("mscore",)),
        ("navigate", (), ("mscore",)),
        ("practice", (), ("mscore",)),
        ("preflight", (), (sys.executable,)),
        ("publish", (), ("mscore", "git", "git-filter-repo", "homr", "pdftotext")),
        ("status", (), ("git",)),
        ("verify", (), ("mscore",)),
    ],
)
def test_entry_proves_its_tools_before_work(
    name: str,
    arguments: tuple[str, ...],
    tools: tuple[str, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refuse the last dependency with no input paths supplied and name that tool."""
    seen: list[str | None] = []

    def answers(command: str | None) -> bool:
        seen.append(command)
        return command != tools[ENTRY_LAST_TOOL]

    def mscore_command() -> str:
        return "mscore"

    def homr_command() -> str:
        return "homr"

    def which(command: str) -> str:
        return command

    monkeypatch.setattr(launch, "tool_answers", answers)
    monkeypatch.setattr(launch, "mscore_command", mscore_command)
    monkeypatch.setattr(gate, "homr_command", homr_command)
    monkeypatch.setattr(toolpath, "which", which)
    monkeypatch.setattr(sys, "argv", [f"muse.{name}", *arguments])
    saved = {stop: signal.getsignal(stop) for stop in launch.TERMINAL_STOPS}
    try:
        with pytest.raises(SystemExit) as caught:
            importlib.import_module(f"muse.{name}").main()
        assert tools[ENTRY_LAST_TOOL] in str(caught.value)
        assert seen == list(tools)
    finally:
        for stop, handler in saved.items():
            signal.signal(stop, handler)


def test_checked_failure_includes_stderr_absent_from_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    """Preserve a short tool's stderr and failure status in its raised error."""
    argv = ["fixture-tool", "--read"]

    def run(command: list[str], **_options: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(command, ENTRY_FAILURE_CODE, b"", ENTRY_ERROR_TEXT.encode())

    monkeypatch.setattr(launch.subprocess, "run", run)
    assert all(ENTRY_ERROR_TEXT not in argument for argument in argv)
    with pytest.raises(launch.FailedRunError) as caught:
        launch.checked(argv, ENTRY_TEST_TIMEOUT)
    assert ENTRY_ERROR_TEXT in str(caught.value)
    assert caught.value.returncode == ENTRY_FAILURE_CODE
    assert caught.value.stderr == ENTRY_ERROR_TEXT.encode()


def test_checked_missing_program_names_the_tool(tmp_path: pathlib.Path) -> None:
    """Translate a short tool's startup failure into a machine fault."""
    missing = tmp_path / "missing-tool"
    with pytest.raises(launch.ToolMissingError) as caught:
        launch.checked([str(missing)], ENTRY_TEST_TIMEOUT)
    assert str(missing) in str(caught.value)
    assert isinstance(caught.value.__cause__, FileNotFoundError)


def test_checked_keeps_bytes_and_run_options(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """Keep binary output intact and pass the caller's deadline and directory."""
    argv = ["fixture-tool"]
    calls: list[dict[str, object]] = []

    def run(command: list[str], **options: object) -> subprocess.CompletedProcess[bytes]:
        assert command == argv
        calls.append(options)
        return subprocess.CompletedProcess(command, ENTRY_SUCCESS_CODE, ENTRY_BINARY, b"")

    monkeypatch.setattr(launch.subprocess, "run", run)
    done = launch.checked(argv, ENTRY_TEST_TIMEOUT, cwd=tmp_path)
    assert done.stdout == ENTRY_BINARY
    assert calls == [
        {
            "capture_output": True,
            "check": False,
            "cwd": tmp_path,
            "stdin": subprocess.DEVNULL,
            "timeout": ENTRY_TEST_TIMEOUT,
        },
    ]


def test_short_callers_keep_output_and_deadlines(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """Route text and binary consumers through checked without changing their deadlines."""
    calls: list[tuple[list[str], float | None, pathlib.Path | None]] = []

    def checked(
        argv: list[str],
        timeout: float | None,
        *,
        cwd: pathlib.Path | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append((argv, timeout, cwd))
        output = ENTRY_BINARY if "show" in argv else b"text\n"
        return subprocess.CompletedProcess(argv, ENTRY_SUCCESS_CODE, output, b"")

    def which(command: str) -> str:
        return command

    def title_problems(_pdf: pathlib.Path, _title: str) -> list[str]:
        return []

    monkeypatch.setattr(launch, "checked", checked)
    monkeypatch.setattr(toolpath, "which", which)
    monkeypatch.setattr(verify, "title_problems", title_problems)
    monkeypatch.setenv("MUSE_PRIVATE", str(tmp_path))
    (tmp_path / verify.PRIVATE_BANNED).write_text("", encoding="utf-8")
    assert publish.sh("git", "status", cwd=tmp_path) == "text"
    assert status.blob(tmp_path, "song.pdf") == ENTRY_BINARY
    pdf = tmp_path / "song.pdf"
    assert verify.pdf_words_problems(pdf, "Title") == []
    assert calls == [
        (["git", "status"], publish.SH_TIMEOUT_SECONDS, tmp_path),
        (["git", "-C", str(tmp_path), "show", "HEAD:song.pdf"], None, None),
        (["pdftotext", str(pdf), "-"], None, None),
    ]


def test_song_worker_start_failure_is_a_missing_tool(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """Keep a worker startup refusal distinct from a song failure."""

    def start(_argv: list[str], **_options: object) -> subprocess.Popen[bytes]:
        raise FileNotFoundError(sys.executable)

    class SongIdentity:
        """Supply the worker's song name without decoding a PDF."""

        name = "Song"

    def song_of(_src: pathlib.Path) -> SongIdentity:
        return SongIdentity()

    monkeypatch.setattr(build, "song_of", song_of)
    monkeypatch.setattr(build.subprocess, "Popen", start)
    with pytest.raises(launch.ToolMissingError) as caught:
        build.child(tmp_path / "source.pdf", tmp_path, tmp_path)
    assert sys.executable in str(caught.value)
    assert isinstance(caught.value.__cause__, FileNotFoundError)
