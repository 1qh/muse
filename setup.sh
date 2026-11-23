#!/bin/sh
# Bring the Python environment to the newest Python on which every dependency installs at its newest release.
set -eu
root=$(cd "$(dirname "$0")" && pwd -P)
for python in $(uv python list --all-versions --output-format json | python3 "${root}/tools/pythons.py"); do
  rm -rf "${root}/.venv.new"
  uv venv -q --relocatable --python "${python}" "${root}/.venv.new"
  if VIRTUAL_ENV="${root}/.venv.new" uv pip install -q --refresh --upgrade -e "${root}[dev]" 2> /dev/null \
    && uv pip list --outdated --format json --python "${root}/.venv.new/bin/python" | python3 "${root}/tools/outdated.py" > /dev/null; then # a Python that installs only an older release of some dependency is not the one
    rm -rf "${root}/.venv"
    mv "${root}/.venv.new" "${root}/.venv"
    echo ok
    exit 0
  fi
done
rm -rf "${root}/.venv.new"
echo "no released Python installs every dependency at its newest release" >&2
exit 1
