"""Every word a source prints must reach the decoded score: a check independent of the decoder's reading."""
from __future__ import annotations

import collections
import os
import pathlib
import re
import unicodedata

import pymupdf

from muse.model import Score

TOKEN = re.compile(r"[^\W\d_]+")
MUSIC_FONTS = ("Anastasia", "CIDFont+F1", "Bravura", "Leland")
CHORD_PIECES = {"maj", "add", "m", "dim", "sus", "aug", "a", "b", "c", "d", "e", "f", "g"}
CREDIT_LABELS = {"sheet", "by", "copyright", "contact", "sáng", "tác", "trình", "bày", "singer", "lời", "việt"}
CHORD = re.compile(r"[a-g](m|maj|min|sus|dim|add|aug)*")
JUMP_WORDS = {"to", "coda", "fine"}
PRIVATE_NAMES = "banned.txt"


def tokens(text: str) -> list[str]:
    return TOKEN.findall(unicodedata.normalize("NFC", text).replace("Ð", "Đ").lower())


def source_words(path: pathlib.Path) -> collections.Counter[str]:
    words: collections.Counter[str] = collections.Counter()
    for page in pymupdf.open(path):
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    if not span["font"].startswith(MUSIC_FONTS):
                        words.update(tokens(span["text"]))
    return words


def decoded_words(score: Score) -> collections.Counter[str]:
    words: collections.Counter[str] = collections.Counter()
    for bar in score.bars:
        for text in [*bar.texts, *bar.texts2]:
            words.update(tokens("".join(run[0] for run in text.runs)))
        for mark in bar.rehearsal:
            words.update(tokens(mark))
        if bar.tempo:
            words.update(tokens(bar.tempo[0]))
    return words


def lost_words(printed: collections.Counter[str], decoded: collections.Counter[str], excluded: set[str]) -> dict[str, int]:
    """Words printed more often than decoded; a joined word counts by its letters, so it is not lost."""
    joined = "".join(decoded)
    return {word: count - decoded[word] for word, count in printed.items() if count > decoded[word] and word not in excluded and word not in joined and not CHORD.fullmatch(word)}


def problems(path: pathlib.Path, score: Score, title: str) -> list[str]:
    names = pathlib.Path(os.environ["MUSE_PRIVATE"]) / PRIVATE_NAMES
    private = {word for line in names.read_text(encoding="utf-8").splitlines() for word in tokens(line)}
    lost = lost_words(source_words(path), decoded_words(score), private | set(tokens(title)) | CHORD_PIECES | CREDIT_LABELS | JUMP_WORDS)
    return [f"words printed in the source and not decoded: {sorted(lost)}"] if lost else []
