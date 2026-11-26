#!/bin/sh
# Build every song a published sheets repo holds, each alone, then compare each one's sheets with the published ones.
# usage: time-all.sh <sources joined with :> <published sheets repo> <scratch dir>
# Prints 1 timing line per song, then 1 comparison line per song, and exits non-zero when any build or comparison fails.
set -eu
[ "$#" -eq 3 ] || {
  echo "usage: time-all.sh <sources> <published sheets repo> <scratch dir>" >&2
  exit 2
}
sources=$1
published=$2
scratch=$3
here=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
repo=$(dirname -- "${here}")
songs=$(find "${published}" -mindepth 1 -maxdepth 1 -type d ! -name '.*' -exec basename {} \; | sort)
[ -n "${songs}" ] || {
  echo "no song folders in ${published}" >&2
  exit 1
}
status=0
printf '%s\n' "${songs}" | xargs sh "${here}/time-songs.sh" "${sources}" "${scratch}" || status=1
printf '%s\n' "${songs}" | while IFS= read -r song; do
  if (cd "${repo}" && .venv/bin/python tools/same_sheets.py "${published}" "${scratch}/${song}/sheets" "${song}") < /dev/null > /dev/null; then
    echo "${song} same"
  else
    echo "${song} differs"
  fi
done
exit "${status}"
