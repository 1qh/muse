"""A rendered PDF read once per version of the file: every check shares 1 parse of its text and drawings."""

from __future__ import annotations

import collections
import pathlib
import threading
from typing import TYPE_CHECKING, Literal, Protocol, TypedDict, Unpack, cast, overload

import pymupdf

if TYPE_CHECKING:
    from collections.abc import Iterator

KEEP = 64
Box = tuple[float, float, float, float]
Point = tuple[float, float]
Word = tuple[float, float, float, float, str, int, int, int]
LineItem = tuple[Literal["l"], pymupdf.Point, pymupdf.Point]
RectItem = tuple[Literal["re"], pymupdf.Rect, int]
CurveItem = tuple[Literal["c"], pymupdf.Point, pymupdf.Point, pymupdf.Point, pymupdf.Point]
QuadItem = tuple[Literal["qu"], pymupdf.Quad]
Item = LineItem | RectItem | CurveItem | QuadItem
FontRow = tuple[int, str, str, str, str, str]


class Char(TypedDict):
    """1 character of a raw text dictionary."""

    bbox: Box
    c: str
    origin: Point
    synthetic: bool


class Span(TypedDict, total=False):
    """1 run of text in 1 font, size and colour; `text` in a text dictionary, `chars` in a raw one."""

    alpha: int
    ascender: float
    bbox: Box
    bidi: int
    char_flags: int
    chars: list[Char]
    color: int
    descender: float
    flags: int
    font: str
    origin: Point
    size: float
    text: str


class Line(TypedDict):
    """1 line of spans."""

    bbox: Box
    dir: Point
    spans: list[Span]
    wmode: int


class Block(TypedDict, total=False):
    """1 block of lines; an image block carries no lines."""

    bbox: Box
    flags: int
    lines: list[Line]
    number: int
    type: int


class TextPage(TypedDict):
    """A page's text dictionary."""

    blocks: list[Block]
    height: float
    width: float


class Drawing(TypedDict, total=False):
    """1 vector path: its drawing commands and how it is stroked and filled."""

    closePath: bool
    color: tuple[float, ...] | None
    dashes: str
    even_odd: bool
    fill: tuple[float, ...] | None
    fill_opacity: float
    items: list[Item]
    layer: str
    lineCap: tuple[int, ...]
    lineJoin: float
    rect: pymupdf.Rect
    seqno: int
    stroke_opacity: float
    type: str
    width: float | None


class PixmapOptions(TypedDict, total=False):
    """Options accepted when rendering a page."""

    matrix: pymupdf.Matrix
    dpi: int
    colorspace: pymupdf.Colorspace
    clip: pymupdf.Rect
    alpha: bool
    annots: bool


class RawPage(Protocol):
    """What a reader needs of a page: its raw text, its drawings and its box."""

    def get_text(self, kind: Literal["rawdict"]) -> TextPage:
        """Return the page's raw text dictionary."""

    def get_drawings(self) -> list[Drawing]:
        """Return every vector path the page draws."""

    @property
    def rect(self) -> pymupdf.Rect:
        """The page's box."""


class Page:
    """1 page whose text and drawings are parsed at most once per kind."""

    def __init__(self, page: pymupdf.Page) -> None:
        self.page = page
        self.texts: dict[tuple[str, int | None], object] = {}
        self.drawings: list[Drawing] | None = None
        self.lock = threading.Lock()

    @overload
    def get_text(self, kind: Literal["dict", "rawdict"], flags: int | None = None) -> TextPage: ...

    @overload
    def get_text(self, kind: Literal["words"], flags: int | None = None) -> list[Word]: ...

    @overload
    def get_text(self, kind: Literal["text"] = "text", flags: int | None = None) -> str: ...

    def get_text(self, kind: str = "text", flags: int | None = None) -> TextPage | list[Word] | str:
        """Parse once per kind and flags: flags change the boxes, so each set is its own parse.

        Returns:
            The page text of that kind, parsed on the first call and shared after.

        """
        with self.lock:
            key = (kind, flags)
            if key not in self.texts:
                self.texts[key] = self.page.get_text(kind) if flags is None else self.page.get_text(kind, flags=flags)
            return cast("TextPage | list[Word] | str", self.texts[key])

    def get_drawings(self) -> list[Drawing]:
        """Parse the page's vector paths once.

        Returns:
            Every path the page draws.

        """
        with self.lock:
            if self.drawings is None:
                self.drawings = cast("list[Drawing]", self.page.get_drawings())
            return self.drawings

    @property
    def rect(self) -> pymupdf.Rect:
        """The page's box."""
        return pymupdf.Rect(self.page.rect)

    @property
    def number(self) -> int:
        """The page's index in its document."""
        return int(self.page.number or 0)

    def get_pixmap(self, **options: Unpack[PixmapOptions]) -> pymupdf.Pixmap:
        """Render the page.

        Returns:
            The rendered page, with PyMuPDF's own rendering options.

        """
        return self.page.get_pixmap(**options)

    def get_fonts(self) -> list[FontRow]:
        """List the fonts the page uses.

        Returns:
            PyMuPDF's own font rows: number, extension, type, base name, name and encoding.

        """
        return [cast("FontRow", tuple(font[:6])) for font in self.page.get_fonts()]

    def search_for(self, text: str) -> list[pymupdf.Rect]:
        """Find where a string prints.

        Returns:
            The box of every place it prints.

        """
        return [pymupdf.Rect(rect) for rect in self.page.search_for(text)]


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


def read(path: pathlib.Path | str) -> Document:
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
