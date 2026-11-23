"""A rendered PDF read once per version of the file: every check shares 1 parse of its text and drawings."""

from __future__ import annotations

import collections
import pathlib
import threading
from typing import TYPE_CHECKING, Any

import pymupdf

if TYPE_CHECKING:
    from collections.abc import Iterator

KEEP = 64


class Page:
    """1 page whose text and drawings are parsed at most once per kind."""

    def __init__(self, page: pymupdf.Page) -> None:
        self.page = page
        self.texts: dict[tuple[str, int | None], Any] = {}
        self.drawings: list[Any] | None = None
        self.lock = threading.Lock()

    def get_text(self, kind: str = "text", flags: int | None = None) -> Any:
        """Parse once per kind and flags: flags change the boxes, so each set is its own parse.

        Returns:
            The page text of that kind, parsed on the first call and shared after.

        """
        with self.lock:
            key = (kind, flags)
            if key not in self.texts:
                self.texts[key] = self.page.get_text(kind) if flags is None else self.page.get_text(kind, flags=flags)
            return self.texts[key]

    def get_drawings(self) -> list[Any]:
        with self.lock:
            if self.drawings is None:
                self.drawings = self.page.get_drawings()
            return self.drawings

    def __getattr__(self, name: str) -> Any:
        return getattr(self.page, name)


class Document:
    """The pages of 1 version of a PDF."""

    def __init__(self, path: pathlib.Path) -> None:
        self.doc = pymupdf.open(path)
        self.pages = [Page(page) for page in self.doc]

    def __iter__(self) -> Iterator[Page]:
        return iter(self.pages)

    def __len__(self) -> int:
        return len(self.pages)

    def __getitem__(self, index: int) -> Page:
        return self.pages[index]


CACHE: collections.OrderedDict[tuple[str, int, int], Document] = collections.OrderedDict()
LOCK = threading.Lock()


def open(path: pathlib.Path | str) -> Document:  # ruff: ignore[builtin-variable-shadowing] - the name the checks already call
    """Return the shared parse of a PDF, keyed by its path, size and change time so a rewrite is read fresh.

    Returns:
        The cached document for this version of the file.

    """
    stat = pathlib.Path(path).stat()
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    with LOCK:
        if key in CACHE:
            CACHE.move_to_end(key)
            return CACHE[key]
    doc = Document(pathlib.Path(path))
    with LOCK:
        CACHE[key] = doc
        while len(CACHE) > KEEP:
            CACHE.popitem(last=False)
    return doc
