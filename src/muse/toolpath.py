"""Package-manager folders on the path, for runs started over ssh or by a scheduler with no login path.

Such a run would read git-filter-repo, homr's helpers or pdftotext as missing; importing this module adds their
folders where they exist and are absent before any tool is looked up.
"""

import os
import pathlib
import shutil

PACKAGE_PATHS = ("/opt/homebrew/bin", "/usr/local/bin")


def with_package_paths() -> None:
    """Append each package-manager folder that exists and is not on the path yet."""
    held = os.environ.get("PATH", "").split(os.pathsep)
    extra = [folder for folder in PACKAGE_PATHS if folder not in held and pathlib.Path(folder).is_dir()]
    if extra:
        os.environ["PATH"] = os.pathsep.join([*filter(None, held), *extra])


def which(name: str) -> str | None:
    """Find a tool on the path, package-manager folders included.

    Returns:
        The tool's path, or none.

    """
    with_package_paths()
    return shutil.which(name)
