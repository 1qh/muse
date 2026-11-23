#!/bin/sh
# Bring the machine to the pipeline's state: the Python environment at the newest Python and dependencies, and MuseScore 3.7 at the newest build of its continuous release, native on Apple silicon.
set -eu
root=$(cd "$(dirname "$0")" && pwd -P)
python=3.14  # the newest Python every dependency ships for; the pipeline ADR names what moves it
rm -rf "$root/.venv.new"
uv venv -q --python "$python" "$root/.venv.new"
VIRTUAL_ENV="$root/.venv.new" uv pip install -q --refresh --upgrade -e "${root}[dev]"
rm -rf "$root/.venv"
mv "$root/.venv.new" "$root/.venv"
app="$HOME/Applications/MuseScore 3.7.app"
repo="Jojo-Schmitz/MuseScore"
dir=$(cd "$(mktemp -d)" && pwd -P)  # the resolved path, as mount reports it
cleanup() {
  if mount | grep -q " on $dir/mnt "; then
    diskutil eject "$dir/mnt" >/dev/null || { echo "could not eject $dir/mnt; left in place" >&2; return; }
  fi
  rm -rf "$dir"
}
trap cleanup EXIT
gh release download continuous -R "$repo" -p "*-Apple.dmg" -D "$dir" >/dev/null
mkdir "$dir/mnt"
diskutil image attach --mountOptions nobrowse --readOnly --mountPoint "$dir/mnt" "$dir"/*.dmg >/dev/null
found=$(find "$dir/mnt" -maxdepth 1 -name "*.app" | head -n 1)
[ -n "$found" ] || { echo "no app inside the $repo Apple image" >&2; exit 1; }
file "$found/Contents/MacOS/mscore" | grep -q arm64 || { echo "$found is not native on Apple silicon" >&2; exit 1; }
mkdir -p "$HOME/Applications"
rm -rf "$app.new"
cp -R "$found" "$app.new"
rm -rf "$app"
mv "$app.new" "$app"
echo ok
