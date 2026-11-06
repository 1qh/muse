"""Print mode: the owner's temporary looser polish rules for a rehearsal; the strict values stay beside them and return when the mode ends."""
from __future__ import annotations

import os

PRINT = os.environ.get("MUSE_PRINT_MODE") == "1"


def pick(strict: float, loose: float) -> float:
    """The rule's strict value, or its print-mode value while that mode is on."""
    return loose if PRINT else strict


def label() -> str:
    """What every build key carries, so a print-mode sheet is never reused as strict."""
    return "print" if PRINT else "strict"
