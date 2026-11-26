"""Measurement reuse and the outputs measurement actually consumes."""

import pathlib
import zipfile
from collections import OrderedDict
from dataclasses import dataclass, field
from fractions import Fraction
from typing import override

import pytest

from muse import layout, port
from muse.model import Bar, Score

BAR_NUMBER = 1
PAGE_NUMBER = 0
BAR_LEFT = 0.0
BAR_WIDTH = 100.0
BAR_LENGTH = 4
CHORD_SHARE = 2.0
CHANGED_SIZE = 1.8
RENDER_COUNT = 2
REMEASURE_RENDER_COUNT = RENDER_COUNT + RENDER_COUNT
EXTENSION_COUNT = 1
IDENTITY_COUNT = 1
REMEASURE_EXTENSION_COUNT = EXTENSION_COUNT + EXTENSION_COUNT
ZIP_FIRST_TIME = (2000, 1, 1, 0, 0, 0)
ZIP_NEXT_TIME = (2001, 1, 1, 0, 0, 0)


class MeasuringSheet(layout.Sheet):
    """A sheet whose prepared score and position output are controlled by the test."""

    @override
    def __post_init__(self) -> None:
        self.name = "measurement"
        self.options = None
        self.score = Score(
            [Bar(BAR_NUMBER, PAGE_NUMBER, BAR_LEFT, BAR_WIDTH, Fraction(BAR_LENGTH), new_system=True, new_page=True)],
            [],
            [],
        )

    @override
    def imported(self) -> pathlib.Path:
        return self.src


@dataclass
class MeasuringBatcher:
    """Write a position output and record every export requested."""

    outputs: list[list[pathlib.Path]] = field(default_factory=list)

    def render(
        self,
        _score: pathlib.Path,
        outputs: list[pathlib.Path],
        _options: port.TransposeOptions | None,
    ) -> None:
        self.outputs.append(list(outputs))
        for output in outputs:
            output.write_text(
                f'<elements><element x="{BAR_LEFT}" y="{BAR_LEFT}" sx="{BAR_WIDTH}" page="{PAGE_NUMBER}"/></elements>',
                encoding="utf-8",
            )


@dataclass
class MeasurementProbe:
    """The sheet and the expensive calls made while measuring it."""

    sheet: MeasuringSheet
    batcher: MeasuringBatcher
    extensions: list[pathlib.Path]


@pytest.fixture
def measurement_probe(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> MeasurementProbe:
    source = tmp_path / "source.mscz"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("score.mscx", "<Score/>")

    def edited(
        imported: pathlib.Path,
        target: pathlib.Path,
        size: float,
        distance: float,
        _score: Score,
    ) -> None:
        with zipfile.ZipFile(imported) as old, zipfile.ZipFile(target, "w") as new:
            for name in old.namelist():
                new.writestr(name, old.read(name))
            new.writestr("style.mss", f"<Style><Spatium>{size}</Spatium><distance>{distance}</distance></Style>")

    extensions: list[pathlib.Path] = []

    def shares(natural: pathlib.Path, _bars: int, _noted: list[int]) -> list[float]:
        extensions.append(natural)
        return [CHORD_SHARE]

    batcher = MeasuringBatcher()
    monkeypatch.setattr(layout, "BATCHER", batcher)
    monkeypatch.setattr(layout, "MEASUREMENTS", OrderedDict())
    monkeypatch.setattr(layout.port, "edited", edited)
    monkeypatch.setattr(layout, "chord_shares", shares)
    sheet = MeasuringSheet(source, tmp_path, port.ORIGINAL, port.SIZE_FLOORS[PAGE_NUMBER])
    return MeasurementProbe(sheet, batcher, extensions)


def test_identical_measurements_reuse_results_and_restore_shares(measurement_probe: MeasurementProbe) -> None:
    sheet, batcher, extensions = measurement_probe.sheet, measurement_probe.batcher, measurement_probe.extensions
    widths, room = layout.measure(sheet)
    assert widths == [BAR_WIDTH * CHORD_SHARE]
    assert room == BAR_WIDTH
    widths.clear()
    sheet.shares.clear()
    repeated = MeasuringSheet(sheet.src, sheet.work, sheet.variant, sheet.size)
    assert layout.measure(repeated) == ([BAR_WIDTH * CHORD_SHARE], BAR_WIDTH)
    assert repeated.shares == [CHORD_SHARE]
    assert all(bar.new_system and bar.new_page for bar in repeated.score.bars)
    assert len(batcher.outputs) == RENDER_COUNT
    assert len(extensions) == EXTENSION_COUNT


def test_measurement_exports_only_positions(measurement_probe: MeasurementProbe) -> None:
    sheet = measurement_probe.sheet
    layout.measure(sheet)
    assert measurement_probe.batcher.outputs == [
        [sheet.path("natural").with_suffix(".mpos")],
        [sheet.path("normal").with_suffix(".mpos")],
    ]


def test_measurement_identity_ignores_archive_timestamps(measurement_probe: MeasurementProbe) -> None:
    sheet = measurement_probe.sheet
    layout.measure(sheet)
    natural, normal = sheet.path("natural"), sheet.path("normal")
    identities: list[str] = []
    for stamp in (ZIP_FIRST_TIME, ZIP_NEXT_TIME):
        for path in (natural, normal):
            with zipfile.ZipFile(path) as old:
                contents = [(item.filename, old.read(item)) for item in old.infolist()]
            with zipfile.ZipFile(path, "w") as new:
                for name, data in contents:
                    new.writestr(zipfile.ZipInfo(name, stamp), data)
        identities.append(layout._measurement_key(natural, normal, sheet.options, []))
    assert len(set(identities)) == IDENTITY_COUNT


@pytest.mark.parametrize("changed", ["source", "size", "distance", "options", "noted"])
def test_changed_measurement_inputs_are_measured_again(
    measurement_probe: MeasurementProbe,
    monkeypatch: pytest.MonkeyPatch,
    changed: str,
) -> None:
    sheet = measurement_probe.sheet
    layout.measure(sheet)
    if changed == "source":
        with zipfile.ZipFile(sheet.src, "w") as archive:
            archive.writestr("score.mscx", "<Score><Measure/></Score>")
    elif changed == "size":
        sheet.size = CHANGED_SIZE
    elif changed == "distance":
        sheet.distance += layout.GAP_STEP
    elif changed == "options":
        sheet.options = {
            "direction": "up",
            "mode": "to_key",
            "targetKey": PAGE_NUMBER,
            "transposeChordNames": True,
            "transposeInterval": PAGE_NUMBER,
            "transposeKeySignatures": True,
            "useDoubleSharpsFlats": False,
        }
    else:
        monkeypatch.setattr(layout.piano, "has_notes", lambda _bar: True)
    assert layout.measure(sheet) == ([BAR_WIDTH * CHORD_SHARE], BAR_WIDTH)
    assert len(measurement_probe.batcher.outputs) == REMEASURE_RENDER_COUNT
    assert len(measurement_probe.extensions) == REMEASURE_EXTENSION_COUNT
