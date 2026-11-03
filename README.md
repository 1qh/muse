# muse

Rebuilds vector PDF lead sheets as MuseScore scores in a chosen key and exports clean PDFs. Source sheets and outputs are never stored here. Design lives in `muse-doc`.

```sh
uv venv --python 3.12 && uv pip install -e '.[dev]'
git config core.hooksPath hooks
PYTHONPATH=src .venv/bin/python -m muse.port <source-dir> <out-dir> <work-dir>
PYTHONPATH=src MUSE_HOMR=<homr-binary> .venv/bin/python -m muse.gate <source-dir> <out-dir> <work-dir>
PYTHONPATH=src MUSE_HOMR=<homr-binary> .venv/bin/python -m muse.publish <source-dir> <out-dir> <work-dir>
```

## Adding a sheet

Name the PDF `<Name>-<Key>`, such as `Song-Cm.pdf`, and put it in the source folder. The target key and title are derived; an unusable title goes in `titles.json` beside the sources as `{"<Name>": "<Title>"}`. A name that disagrees with the printed key, an unknown music encoding, an undecoded music symbol, or a missing SF Pro Display is refused by name. SF Pro Display installs into `~/Library/Fonts`.

## Publishing

`muse.publish` is the only route to the sheets repo: it refuses unless the repo is private and the gate passes, then pushes and purges replaced binaries from its history.

## Checks

CI runs lint, types and the unit tests. The sheets are private, so source tests, mutation controls and the gate run locally:

```sh
export MUSE_SOURCES=<source-dir> MUSE_PINS=<spike-repo>/private/pins.json MUSE_PRIVATE=<spike-repo>/private
.venv/bin/python -m pytest
.venv/bin/python tests/mutate.py
```
