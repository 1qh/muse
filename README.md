# muse

Rebuilds vector PDF lead sheets as MuseScore scores in a chosen key and exports clean PDFs. Source sheets and outputs are never stored here. Design lives in `muse-doc`.

```sh
uv venv --python 3.12 && uv pip install -e .
PYTHONPATH=src .venv/bin/python -m muse.port <source-dir> <out-dir> <work-dir>
PYTHONPATH=src .venv/bin/python -m muse.verify <source-dir> <out-dir> <work-dir>
```

## Checks
```sh
uv pip install -e '.[dev]'
MUSE_SOURCES=<source-dir> .venv/bin/python -m pytest
MUSE_SOURCES=<source-dir> .venv/bin/python tests/mutate.py
.venv/bin/ruff check src tests && .venv/bin/mypy src
PYTHONPATH=src .venv/bin/python -m muse.navigate <source-dir> <out-dir> <work-dir>
PYTHONPATH=src .venv/bin/python -m muse.inverse <source-dir> <out-dir> <work-dir>
PYTHONPATH=src .venv/bin/python -m muse.pdfcheck <out-dir>
```
