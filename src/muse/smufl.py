"""The SMuFL code points and drawing geometry every reader of a SMuFL-engraved page shares.

The readers are the Leland and Bravura sources, and the checks reading the pipeline's own prints.
"""

from __future__ import annotations

from fractions import Fraction

BAR_REST = 0xE4E3
RESTS = {0xE4E4: Fraction(2), 0xE4E5: Fraction(1), 0xE4E6: Fraction(1, 2), 0xE4E7: Fraction(1, 4)}
HEAD_CODES = (0xE0A0, 0xE0FF)
FERMATAS = (0xE4C0, 0xE4C1)
PRIVATE_USE = range(0xE000, 0xF900)
TIME_DIGITS = range(0xE080, 0xE08A)
TIME_SIGNATURES = range(0xE080, 0xE0A0)
FLAT_BEAM_WIDTH = 0.01


def beam_y(beam: tuple[float, float, float, float], x: float) -> float:
    """Return the middle of a slanted beam (left, right, left height, right height) at x.

    Returns:
        The beam's height at x, clamped to its ends.

    """
    x0, x1, y0, y1 = beam
    return y0 if x1 - x0 < FLAT_BEAM_WIDTH else y0 + (y1 - y0) * (min(max(x, x0), x1) - x0) / (x1 - x0)
