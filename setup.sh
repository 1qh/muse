#!/bin/sh
# Install or refresh MuseScore 3.7 at the newest build of its continuous release, native on Apple silicon.
set -eu
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
