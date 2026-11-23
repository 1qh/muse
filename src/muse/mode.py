"""Print mode: the owner's temporary looser polish rules for a rehearsal.

The strict values stay beside them and return when the mode ends.
"""

from __future__ import annotations

import os

PRINT = os.environ.get("MUSE_PRINT_MODE") == "1"


def pick(strict: float, loose: float) -> float:
    """Return the rule's strict value, or its print-mode value while that mode is on.

    Returns:
        The loose value in print mode, the strict value otherwise.

    """
    return loose if PRINT else strict


def label() -> str:
    """Name the mode every build key carries, so a print-mode sheet is never reused as strict.

    Returns:
        `print` in print mode, `strict` otherwise.

    """
    return "print" if PRINT else "strict"
