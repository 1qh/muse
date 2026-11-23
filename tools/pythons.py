"""Every released CPython minor version uv can install, newest first.

Read from `uv python list --output-format json` on stdin.
"""

import json
import re
import sys

found = {
    f"{entry['version_parts']['major']}.{entry['version_parts']['minor']}"
    for entry in json.load(sys.stdin)
    if entry["implementation"] == "cpython"
    and entry["variant"] == "default"
    and re.fullmatch(r"\d+\.\d+\.\d+", entry["version"])
}
sys.stdout.write(" ".join(sorted(found, key=lambda version: tuple(map(int, version.split("."))), reverse=True)) + "\n")
