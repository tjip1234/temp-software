#!/usr/bin/env bash
# Build TjipTemp-<version>-<arch>.AppImage.
#
#     ./packaging/linux/build_appimage.sh
#
# One file, no installation, runs on any glibc-based distribution new enough to
# match the build host. That last part is the catch with AppImages: glibc is not
# forward-compatible, so an image built on a recent distribution will not start
# on an older one. Build on the oldest distribution you intend to support —
# the release workflow uses an old Ubuntu for exactly this reason.
#
# APPIMAGETOOL=/path/to/appimagetool skips the download.
set -euo pipefail

root="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$root"

arch="$(uname -m)"

# Which Python builds the bundle decides which Python ends up *inside* it, so
# resolve it explicitly rather than inheriting whatever "python3" means today.
# A system Python is often externally managed (PEP 668) and will refuse to
# install into itself; an activated virtualenv, or the project's own .venv, is
# almost always what is meant.
if [ -z "${PYTHON:-}" ]; then
  if [ -n "${VIRTUAL_ENV:-}" ] && [ -x "$VIRTUAL_ENV/bin/python" ]; then
    PYTHON="$VIRTUAL_ENV/bin/python"
  elif [ -x "$root/.venv/bin/python" ]; then
    PYTHON="$root/.venv/bin/python"
  else
    PYTHON="$(command -v python3)"
  fi
fi
echo "building with $PYTHON ($("$PYTHON" --version 2>&1))"

# -m PyInstaller, never the bare command: a pyinstaller earlier on PATH would
# bundle a different interpreter than the one holding the dependencies.

version="$("$PYTHON" -c 'import tomllib,pathlib;print(tomllib.loads(pathlib.Path("pyproject.toml").read_text())["project"]["version"])')"
out="dist/TjipTemp-${version}-${arch}.AppImage"

"$PYTHON" -m pip install --upgrade pyinstaller
"$PYTHON" -m pip install -e .

# The spec refuses to embed an icon it cannot open, so generate them first.
"$PYTHON" packaging/make_icons.py

rm -rf build dist AppDir
"$PYTHON" -m PyInstaller packaging/tjiptemp.spec --noconfirm

[ -d dist/tjiptemp ] || { echo "PyInstaller did not produce dist/tjiptemp" >&2; exit 1; }

# ---------------------------------------------------------------- AppDir tree
mkdir -p AppDir/usr/bin AppDir/usr/share/applications \
         AppDir/usr/share/metainfo AppDir/usr/share/icons/hicolor
cp -a dist/tjiptemp/. AppDir/usr/bin/

cp packaging/linux/tjiptemp.desktop AppDir/usr/share/applications/io.github.tjiptemp.desktop
cp packaging/linux/io.github.tjiptemp.metainfo.xml AppDir/usr/share/metainfo/

for size in 16 32 48 64 128 256 512; do
  dir="AppDir/usr/share/icons/hicolor/${size}x${size}/apps"
  mkdir -p "$dir"
  cp "packaging/icon-${size}.png" "$dir/io.github.tjiptemp.png"
done

# appimagetool looks for these three at the AppDir root specifically.
cp packaging/icon-256.png AppDir/io.github.tjiptemp.png
ln -sf io.github.tjiptemp.png AppDir/.DirIcon
cp AppDir/usr/share/applications/io.github.tjiptemp.desktop AppDir/

cat > AppDir/AppRun <<'APPRUN'
#!/bin/sh
# Bundled Qt must not be mixed with the host's: a distribution that happens to
# have a different PySide6 on the library path produces symbol errors that look
# like application bugs.
HERE="$(dirname "$(readlink -f "$0")")"
export PATH="$HERE/usr/bin:$PATH"
unset QT_PLUGIN_PATH QML2_IMPORT_PATH LD_PRELOAD
exec "$HERE/usr/bin/tjiptemp" "$@"
APPRUN
chmod +x AppDir/AppRun

# ------------------------------------------------------------- appimagetool
tool="${APPIMAGETOOL:-}"
if [ -z "$tool" ]; then
  tool="build/appimagetool-${arch}.AppImage"
  if [ ! -x "$tool" ]; then
    mkdir -p build
    echo "fetching appimagetool…"
    curl -fsSL -o "$tool" \
      "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-${arch}.AppImage"
    chmod +x "$tool"
  fi
fi

mkdir -p dist
# ARCH is not inferred reliably; --appimage-extract-and-run avoids needing FUSE,
# which containers and CI runners generally do not have.
ARCH="$arch" "$tool" --appimage-extract-and-run AppDir "$out"

rm -rf AppDir
echo "built: $out"
