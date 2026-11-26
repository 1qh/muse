#!/bin/sh
# Time songs built one after another, each alone, into scratch folders that never touch the published sheets or their records.
# usage: time-songs.sh <sources joined with :> <scratch dir> <song>...
set -eu
[ "$#" -ge 3 ] || {
  echo "usage: time-songs.sh <sources> <scratch dir> <song>..." >&2
  exit 2
}
[ -n "${MUSE_MSCORE:-}" ] && [ -x "${MUSE_MSCORE}" ] || {
  echo "MUSE_MSCORE must name a MuseScoreLite executable" >&2
  exit 2
}
sources=$1
scratch=$2
shift 2
repo=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
for song in "$@"; do
  run="${scratch}/${song}"
  rm -rf "${run}"
  mkdir -p "${run}/state"
  git init -q "${run}/sheets"
  start=$(date +%s)
  status=0
  (cd "${repo}" && MUSE_BUILD_STATE="${run}/state" PYTHONPATH=src .venv/bin/python -m muse.build "${sources}" "${run}/sheets" "${run}/work" "${song}") > "${run}/build.log" 2>&1 < /dev/null || status=$?
  end=$(date +%s)
  echo "${song} $((end - start)) s exit ${status}"
done
