"""The SMuFL code points and drawing geometry every reader of a SMuFL-engraved page shares: Leland and Bravura sources, and the checks reading the pipeline's own prints."""
from __future__ import annotations

from fractions import Fraction as F

BAR_REST = 0xE4E3  # a whole-bar rest
RESTS = {0xE4E4: F(2), 0xE4E5: F(1), 0xE4E6: F(1, 2), 0xE4E7: F(1, 4)}  # half, quarter, eighth and 16th rests, in quarters
HEAD_CODES = (0xE0A0, 0xE0FF)  # the noteheads block


def beam_y(beam: tuple[float, float, float, float], x: float) -> float:
    """The middle of a slanted beam (left, right, left height, right height) at x."""
    x0, x1, y0, y1 = beam
    return y0 if x1 - x0 < 0.01 else y0 + (y1 - y0) * (min(max(x, x0), x1) - x0) / (x1 - x0)
