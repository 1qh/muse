"""Compare every PDF of built song folders with the published ones, page by page and pixel by pixel.

usage: python tools/same_sheets.py <published sheets repo> <built sheets folder> <song>...
Prints 1 line per sheet and `ok` when every sheet of every named song is identical; exits non-zero otherwise.
"""

import pathlib
import sys

import pymupdf

DPI = 100
MIN_ARGS = 3


def page_pixels(pdf: pathlib.Path) -> list[bytes]:
    """Render every page of a PDF.

    Args:
        pdf: The PDF to render.

    Returns:
        The raw pixels of each page, in order.

    """
    with pymupdf.open(pdf) as document:
        return [page.get_pixmap(dpi=DPI).samples for page in document]


def differences(published: pathlib.Path, built: pathlib.Path, song: str) -> list[str]:
    """List how a song's built sheets differ from its published ones.

    Args:
        published: The published sheets repo.
        built: The folder holding the built song folders.
        song: The song's folder name.

    Returns:
        1 line per sheet that differs or is missing on either side; empty when all match.

    """
    old = {pdf.name: pdf for pdf in (published / song).glob("*.pdf")}
    new = {pdf.name: pdf for pdf in (built / song).glob("*.pdf")}
    if not old:
        return [f"{song}: no published sheets"]
    found = [f"{song}/{name}: missing from the build" for name in sorted(old.keys() - new.keys())]
    found.extend(f"{song}/{name}: not published" for name in sorted(new.keys() - old.keys()))
    for name in sorted(old.keys() & new.keys()):
        before = page_pixels(old[name])
        after = page_pixels(new[name])
        if len(before) != len(after):
            found.append(f"{song}/{name}: {len(before)} pages published, {len(after)} built")
            continue
        changed = [str(index + 1) for index, pair in enumerate(zip(before, after, strict=True)) if pair[0] != pair[1]]
        if changed:
            found.append(f"{song}/{name}: pages {', '.join(changed)} differ")
    return found


def main() -> int:
    """Compare the songs named on the command line.

    Returns:
        0 when every sheet matches, 1 otherwise, 2 on bad arguments.

    """
    if len(sys.argv) <= MIN_ARGS:
        sys.stderr.write("usage: same_sheets.py <published sheets repo> <built sheets folder> <song>...\n")
        return 2
    published, built = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
    found = [line for song in sys.argv[3:] for line in differences(published, built, song)]
    if found:
        sys.stderr.write("\n".join(found) + "\n")
        return 1
    sys.stdout.write("ok\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
