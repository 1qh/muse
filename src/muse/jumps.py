"""Jump signs and words that a MusicXML import leaves as staff text, rewritten as MuseScore's own playable marker and jump elements."""
from __future__ import annotations

import re
from collections.abc import Callable

SIGN_SIZE = 25  # a printed sign may exceed the chords: about 2.5 times the default sign
LABELS = {"segno": "segno", "coda": "codab"}  # a sign's jump label: the coda sign is where the jump continues

WORDS = {
    "To Coda": f"<Marker><style>repeat_right</style><text><sym>coda</sym></text><label>coda</label><markerType>coda</markerType><markerCenterOnSymbol>0</markerCenterOnSymbol><musicSymbolSize>{SIGN_SIZE}</musicSymbolSize></Marker>",
    "D.S. al Coda": f"<Jump><musicSymbolSize>{SIGN_SIZE}</musicSymbolSize><text><sym>segno</sym></text><jumpTo>segno</jumpTo><playUntil>coda</playUntil><continueAt>codab</continueAt></Jump>",
}


def sign_marker(sym: str) -> str:
    """A visible marker: both the printed sign and the jump target; the style sizes it."""
    return f"<Marker><style>repeat_left</style><text><sym>{sym}</sym></text><label>{LABELS[sym]}</label><markerType>{LABELS[sym]}</markerType><markerCenterOnSymbol>0</markerCenterOnSymbol><musicSymbolSize>{SIGN_SIZE}</musicSymbolSize></Marker>"




def staff_text(inner: str) -> re.Pattern[str]:
    """A staff text whose whole text is exactly this."""
    return re.compile(r"\s*<StaffText>(?:(?!</StaffText>).)*?<text>" + inner + r"</text>\s*</StaffText>", re.S)


def rewrite(text: str, words: dict[str, str], sign: Callable[[str], str], clearance: float | None = None) -> str:
    """Every jump word and sign as a playable element at the start of its measure; with a clearance, a chord beside a sign moves right off it."""
    for word, element in words.items():
        pattern = staff_text(re.escape(word))
        while found := pattern.search(text):
            start = text.rfind("<voice>", 0, found.start())
            text = text[:start] + element + text[start:found.start()] + text[found.end():]
    for sym in LABELS:
        pattern = staff_text(f"<sym>{sym}</sym>")
        for found in reversed(list(pattern.finditer(text))):
            start = text.rfind("<voice>", 0, found.start())
            text = text[:start] + sign(sym) + text[start:found.start()] + text[found.end():]
            chord = text.find("<Harmony>", start)
            if clearance is not None and chord != -1 and chord < text.find("</Measure>", start):  # a marker ignores its own offset in layout, so the chord beside it makes the room
                at = chord + len("<Harmony>")
                text = text[:at] + f'<offset x="{clearance}" y="0"/>' + text[at:]
    return text
