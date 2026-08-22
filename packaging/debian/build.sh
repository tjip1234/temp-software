#!/usr/bin/env bash
# Build the .deb. Run from the repository root:
#     ./packaging/debian/build.sh
#
# Requires: devscripts debhelper dh-python python3-all python3-setuptools
set -euo pipefail

root="$(cd "$(dirname "$0")/../.." && pwd)"
build="$root/build/deb"

rm -rf "$build"
mkdir -p "$build"
# dpkg-buildpackage insists on building in the source tree, so stage a copy.
tar --exclude=./build --exclude=./.git --exclude=./.venv -C "$root" -cf - . \
    | tar -C "$build" -xf -
cp -r "$root/packaging/debian/debian" "$build/debian"

cd "$build"
dpkg-buildpackage -us -uc -b
mv ../tjiptemp_*.deb "$root/build/" 2>/dev/null || true
echo "built: $(ls "$root"/build/tjiptemp_*.deb)"
