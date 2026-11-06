"""The calculated layout's own contracts."""
import pathlib
import types

import pytest

from muse import layout, port


def test_seated_render_refuses_a_missing_score(tmp_path: pathlib.Path) -> None:
    sheet = types.SimpleNamespace(path=lambda label: tmp_path / f"s-{label}.mscz")
    with pytest.raises(port.DoesNotFit, match="edits would be lost"):
        layout.render(sheet, "final", seated=True)  # type: ignore[arg-type]


def test_clear_gap_is_the_chord_gap_in_staff_spaces() -> None:
    chord_points = port.CHORD_SIZE_PER_SPACE  # at a spatium of 1 mm
    space_points = 72 / 25.4
    assert pytest.approx(port.CHORD_GAP * chord_points / space_points) == layout.CLEAR_SPACES
