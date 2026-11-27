"""A tool that cannot start stops the machine's build."""

import contextlib
import errno
import io
import pathlib
from types import TracebackType
from typing import Self

import pytest

from muse import build, gate, launch, render

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


class ReportedChild:
    """Carry a child exit and diagnostics without a refusal file."""

    def __init__(self, code: int, diagnostic: str) -> None:
        self.returncode = code
        self.diagnostic = diagnostic

    def communicate(self) -> tuple[None, bytes]:
        """Read the child's diagnostics.

        Returns:
            No captured stdout and the encoded error.

        """
        return None, self.diagnostic.encode()

    def poll(self) -> int:
        """Read the completed child's status.

        Returns:
            The exit code.

        """
        return self.returncode


def install_disk_child_fault(
    monkeypatch: pytest.MonkeyPatch,
    origin: str,
    fault: OSError,
) -> None:
    """Install faults for song building, gating or refusal-file writing."""

    def song(_src: pathlib.Path, _stage: pathlib.Path, _work: pathlib.Path) -> dict[str, object]:
        if origin == "song":
            raise fault
        if origin == "refusal":
            raise OSError(errno.ENOENT, "missing song file", fault.filename)
        return {"song": SongInfo.name}

    def write_text(
        _path: pathlib.Path,
        _data: str,
        *,
        encoding: str,
    ) -> int:
        del encoding
        raise fault

    def verdicts(
        _src: pathlib.Path,
        _stage: pathlib.Path,
        _work: pathlib.Path,
        _names: tuple[str, ...],
    ) -> dict[str, list[str]]:
        raise fault

    monkeypatch.setattr(build, "song", song)
    monkeypatch.setattr(build.gate, "verdicts", verdicts)
    if origin == "refusal":
        monkeypatch.setattr(pathlib.Path, "write_text", write_text)


@pytest.mark.parametrize("number", [errno.ENOSPC, errno.EDQUOT])
@pytest.mark.parametrize("origin", ["song", "gate", "refusal", "future", "collector"])
def test_disk_capacity_stops_the_run_with_its_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    number: int,
    origin: str,
) -> None:
    """Stop every worker on disk exhaustion without writing a refusal file."""
    target = tmp_path / "full-disk" / "Song.mscz"
    fault = OSError(number, "capacity exhausted", str(target))
    stopped: list[bool] = []
    reports: list[dict[str, object]] = []
    refused: list[str] = []
    blocked: list[str] = []

    def start(_argv: list[str], **_options: object) -> ReportedChild:
        diagnostic = io.StringIO()
        with contextlib.redirect_stderr(diagnostic), pytest.raises(SystemExit) as caught:
            build.child_main(
                tmp_path / "Song-C.pdf",
                tmp_path / "stage",
                tmp_path,
                tmp_path / "src" if origin == "gate" else None,
            )
        assert caught.value.code == build.CHILD_MACHINE_FAULT_EXIT
        machine_fault = caught.value.__cause__
        assert isinstance(machine_fault, launch.MachineUnavailableError)
        assert machine_fault.__cause__ is fault
        return ReportedChild(build.CHILD_MACHINE_FAULT_EXIT, diagnostic.getvalue())

    def child(
        _src: pathlib.Path,
        _stage: pathlib.Path,
        _work: pathlib.Path,
        _gated_from: pathlib.Path | None = None,
    ) -> tuple[dict[str, object] | None, str]:
        if origin == "future":
            raise fault
        return {"song": SongInfo.name}, ""

    def publish_song(
        _src: pathlib.Path,
        _out: pathlib.Path,
        _work: pathlib.Path,
        _stage: pathlib.Path,
        _name: str,
    ) -> str:
        raise fault

    def stop_children() -> None:
        stopped.append(True)

    monkeypatch.setattr(build, "song_of", song_info)
    install_disk_child_fault(monkeypatch, origin, fault)
    monkeypatch.setattr(build.subprocess, "Popen", start)
    monkeypatch.setattr(build, "stop_children", stop_children)
    monkeypatch.setattr(build, "publish_song", publish_song)
    if origin in {"future", "collector"}:
        monkeypatch.setattr(build, "child", child)
    with pytest.raises(SystemExit) as caught:
        build._run(
            [tmp_path / "Song-C.pdf"],
            WORKERS,
            (tmp_path, tmp_path, tmp_path, tmp_path, origin == "collector"),
            (reports, refused, blocked),
            BEGAN,
        )
    assert "disk" in str(caught.value)
    assert str(target) in str(caught.value)
    assert stopped == [True]
    assert refused == []
    assert blocked == []
    assert not (tmp_path / "Song-refusal.txt").exists()
    assert "refused Song" not in capsys.readouterr().out


@pytest.mark.parametrize("origin", ["song", "future"])
def test_an_ordinary_os_error_remains_a_song_refusal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    origin: str,
) -> None:
    """Keep a missing song file local to its song without stopping the run."""
    target = tmp_path / "missing.mscz"
    fault = OSError(errno.ENOENT, "missing song file", str(target))
    stopped: list[bool] = []
    reports: list[dict[str, object]] = []
    refused: list[str] = []
    blocked: list[str] = []

    def song(_src: pathlib.Path, _stage: pathlib.Path, _work: pathlib.Path) -> dict[str, object]:
        raise fault

    def start(_argv: list[str], **_options: object) -> ReportedChild:
        with pytest.raises(SystemExit) as caught:
            build.child_main(tmp_path / "Song-C.pdf", tmp_path / "stage", tmp_path)
        assert caught.value.code == build.CHILD_REFUSED_EXIT
        assert caught.value.__cause__ is fault
        return ReportedChild(build.CHILD_REFUSED_EXIT, "")

    def child(
        _src: pathlib.Path,
        _stage: pathlib.Path,
        _work: pathlib.Path,
        _gated_from: pathlib.Path | None = None,
    ) -> tuple[dict[str, object] | None, str]:
        raise fault

    def stop_children() -> None:
        stopped.append(True)

    monkeypatch.setattr(build, "song_of", song_info)
    monkeypatch.setattr(build, "song", song)
    monkeypatch.setattr(build.subprocess, "Popen", start)
    monkeypatch.setattr(build, "stop_children", stop_children)
    if origin == "future":
        monkeypatch.setattr(build, "child", child)
    build._run(
        [tmp_path / "Song-C.pdf"],
        WORKERS,
        (tmp_path, tmp_path, tmp_path, tmp_path, False),
        (reports, refused, blocked),
        BEGAN,
    )
    assert stopped == []
    assert reports == []
    assert refused == ["Song-C"]
    assert blocked == []
    output = capsys.readouterr().out
    assert "refused Song-C" in output
    assert str(target) in output
    if origin == "song":
        assert (tmp_path / "Song-refusal.txt").read_text(encoding="utf-8") == str(fault)


@pytest.mark.parametrize("number", [errno.ENOSPC, errno.EDQUOT])
@pytest.mark.parametrize("consumer", ["gate", "render"])
def test_checks_and_render_batches_preserve_disk_faults(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    number: int,
    consumer: str,
) -> None:
    """Keep disk faults out of check failures and render refusal messages."""
    target = tmp_path / "Song.mscz"
    fault = OSError(number, "capacity exhausted", str(target))
    failures: list[str] = []
    batcher = render.Batcher()

    def step() -> list[str]:
        raise fault

    def run(_batch: list[render.RenderItem]) -> dict[int, str | None]:
        raise fault

    def consume() -> None:
        if consumer == "gate":
            gate._guarded(failures, "disk check", step)
        else:
            with batcher.worker():
                batcher.render(target, [tmp_path / "Song.pdf"])

    monkeypatch.setattr(batcher, "run", run)
    with pytest.raises(launch.MachineUnavailableError) as caught:
        consume()
    assert caught.value.__cause__ is fault
    assert str(target) in str(caught.value)
    assert failures == []
    assert batcher.done == {}
