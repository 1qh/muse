"""The installed packages behind their newest release, from `uv pip list --outdated --format json` on stdin.

The project itself is not judged, since a package of the same name on PyPI is not it.
"""

import json
import sys

behind = [
    f"{entry['name']} {entry['version']} < {entry['latest_version']}"
    for entry in json.load(sys.stdin)
    if "editable_project_location" not in entry
]
sys.stdout.write("\n".join(behind) + "\n")
raise SystemExit(1 if behind else 0)
