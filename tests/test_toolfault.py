"""A tool that cannot start stops the machine's build."""

import errno
import pathlib
from types import TracebackType
from typing import Self

import pytest

from muse import build, gate, launch

MISSING_TOOL = "/missing/tools/homr"
FAULT_REASON = f"could not start tool {MISSING_TOOL!r}: missing executable"
MARKED_REFUSAL = f"machine fault: {FAULT_REASON}"
WORKERS = 1
BEGAN = 0.0
SUCCESS = 0


class SongInfo:
    """Supply the song identity without reading a source PDF."""

    name = "Song"

    @staticmethod
    def files() -> set[str]:
        """List the empty fixture's required files.

        Returns:
            No files.

        """
        return set()


def song_info(_src: pathlib.Path) -> SongInfo:
    """Resolve the fixture's song identity.

    Returns:
        The fixture song.

    """
    return SongInfo()


def free_core() -> None:
    """Skip the host load check for launch unit tests."""


def test_a_missing_executable_is_a_machine_fault(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """Name the missing executable and preserve the operating system's cause."""
    missing = tmp_path / "missing-homr"
    monkeypatch.setattr(launch, "free_core", free_core)
    with pytest.raises(launch.ToolMissingError) as caught:
        launch.heavy([str(missing)], launch.LAUNCH_SECONDS)
    assert isinstance(caught.value, launch.MachineUnavailableError)
    assert str(missing) in str(caught.value)
    cause = caught.value.__cause__
    assert isinstance(cause, FileNotFoundError)
    assert cause.errno == errno.ENOENT
    assert str(cause) in str(caught.value)


class StartedProcess:
    """Raise an operating system error after a successful process start."""

    returncode = SUCCESS

    def __enter__(self) -> Self:
        """Enter the running process context.

        Returns:
            This process.

        """
        return self

    def __exit__(
        self,
        _kind: type[BaseException] | None,
        _error: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        """Leave the running process context."""

    @staticmethod
    def communicate(*, timeout: float) -> tuple[str, str]:
        """Fail while reading an already running process.

        Args:
            timeout: The requested communication deadline.

        Raises:
            OSError: Reading the running process failed.

        """
        del timeout
        raise OSError(errno.EIO, "reading a started process failed")


def test_an_os_error_after_start_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Classify only process startup errors as missing tools."""

    def start(_argv: list[str], **_options: object) -> StartedProcess:
        return StartedProcess()

    monkeypatch.setattr(launch.subprocess, "Popen", start)
    monkeypatch.setattr(launch, "free_core", free_core)
    with pytest.raises(OSError, match="reading a started process failed") as caught:
        launch.heavy([MISSING_TOOL], launch.LAUNCH_SECONDS)
    assert caught.value.errno == errno.EIO


def test_guarded_propagates_a_missing_tool_without_a_song_failure() -> None:
    """Let a machine fault pass through the song's guard intact."""
    failures: list[str] = []
    fault = launch.ToolMissingError(FAULT_REASON)

    def missing() -> list[str]:
        raise fault

    with pytest.raises(launch.ToolMissingError) as caught:
        gate._guarded(failures, "homr", missing)
    assert caught.value is fault
    assert failures == []


def test_verdicts_propagates_a_missing_tool(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """Keep the outer gate from turning a machine fault into a song verdict."""
    fault = launch.ToolMissingError(FAULT_REASON)

    def sources(_folder: pathlib.Path) -> list[pathlib.Path]:
        return [tmp_path / "Song-C.pdf"]

    def chord_problems(_work: pathlib.Path) -> list[str]:
        return []

    def missing(
        _src: pathlib.Path,
        _out: pathlib.Path,
        _work: pathlib.Path,
    ) -> tuple[list[str], list[str], bool]:
        raise fault

    monkeypatch.setattr(gate, "sources", sources)
    monkeypatch.setattr(gate, "song_of", song_info)
    monkeypatch.setattr(gate, "chord_name_problems", chord_problems)
    monkeypatch.setattr(gate, "cached_check", missing)
    with pytest.raises(launch.ToolMissingError) as caught:
        gate.verdicts(tmp_path, tmp_path / "out", tmp_path)
    assert caught.value is fault


@pytest.fixture
def machine_fault_refusal(tmp_path: pathlib.Path) -> pathlib.Path:
    """Write the child refusal protocol fixture without building a song.

    Returns:
        The marked refusal file.

    """
    fixture = tmp_path / "fixture-refusal.txt"
    fixture.write_text(MARKED_REFUSAL, encoding="utf-8")
    return fixture


@pytest.mark.parametrize("phase", ["song", "gate"])
def test_child_marks_machine_faults_from_building_and_gating(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    machine_fault_refusal: pathlib.Path,
    phase: str,
) -> None:
    """Report machine faults from either child phase using the same marked refusal."""
    fault = launch.ToolMissingError(FAULT_REASON)

    def song(_src: pathlib.Path, _stage: pathlib.Path, _work: pathlib.Path) -> dict[str, object]:
        if phase == "song":
            raise fault
        return {"song": SongInfo.name}

    def verdicts(
        _src: pathlib.Path,
        _stage: pathlib.Path,
        _work: pathlib.Path,
        _names: tuple[str, ...],
    ) -> dict[str, list[str]]:
        raise fault

    monkeypatch.setattr(build, "song_of", song_info)
    monkeypatch.setattr(build, "song", song)
    monkeypatch.setattr(build.gate, "verdicts", verdicts)
    with pytest.raises(SystemExit) as caught:
        build.child_main(tmp_path / "Song-C.pdf", tmp_path / "stage", tmp_path, tmp_path / "src")
    assert caught.value.code == build.CHILD_REFUSED_EXIT
    assert caught.value.__cause__ is fault
    assert (tmp_path / "Song-refusal.txt").read_text(encoding="utf-8") == machine_fault_refusal.read_text(
        encoding="utf-8",
    )


@pytest.mark.parametrize("publishing", [False, True])
def test_parent_stops_all_workers_on_a_marked_child_refusal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    machine_fault_refusal: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    *,
    publishing: bool,
) -> None:
    """Collect a fixture refusal as a run stop and invoke worker cleanup."""
    stopped: list[bool] = []
    purged: list[pathlib.Path] = []
    reports: list[dict[str, object]] = []
    refused: list[str] = []
    blocked: list[str] = []

    def child(
        _src: pathlib.Path,
        _stage: pathlib.Path,
        _work: pathlib.Path,
        _gated_from: pathlib.Path | None = None,
    ) -> tuple[dict[str, object] | None, str]:
        return None, machine_fault_refusal.read_text(encoding="utf-8")

    def stop_children() -> None:
        stopped.append(True)

    def purge(out: pathlib.Path) -> None:
        purged.append(out)

    monkeypatch.setattr(build, "child", child)
    monkeypatch.setattr(build, "stop_children", stop_children)
    monkeypatch.setattr(build.publish, "purge", purge)
    with pytest.raises(SystemExit) as caught:
        build._run(
            [tmp_path / "Song-C.pdf"],
            WORKERS,
            (tmp_path, tmp_path, tmp_path, tmp_path, publishing),
            (reports, refused, blocked),
            BEGAN,
        )
    assert caught.value.code == MARKED_REFUSAL
    assert stopped == [True]
    assert purged == []
    assert reports == []
    assert refused == []
    assert blocked == []
    assert "refused Song" not in capsys.readouterr().out
