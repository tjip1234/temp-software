#!/usr/bin/env bash
# Build TjipTemp-<version>-anylinux-<arch>.AppImage.
#
#     ./packaging/linux/build_anylinux_appimage.sh
#
# The same PyInstaller bundle as build_appimage.sh, but carrying its own glibc
# and every host library it links against, deployed with sharun
# (https://github.com/pkgforge-dev/Anylinux-AppImages). The ordinary AppImage runs
# only on distributions at least as new as its build host; this one does not
# care what the host has, so it can be built anywhere -- the release workflow
# uses an Arch container, which is what quick-sharun is written for.
#
# Needs patchelf, strip and wget on PATH. QUICK_SHARUN=/path/to/quick-sharun
# skips the download.
set -euo pipefail

root="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$root"

arch="$(uname -m)"

# Same interpreter rule as build_appimage.sh.
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

command -v patchelf >/dev/null || { echo "patchelf is required (pip install patchelf)" >&2; exit 1; }

version="$("$PYTHON" -c 'import tomllib,pathlib;print(tomllib.loads(pathlib.Path("pyproject.toml").read_text())["project"]["version"])')"
work="build/anylinux"
bundle="$work/dist/tjiptemp"
appdir="$root/$work/AppDir"
outname="TjipTemp-${version}-anylinux-${arch}.AppImage"
# The Anylinux-AppImages commit quick-sharun is fetched from. Pinned, so a
# release build does not change because an upstream branch moved.
QUICK_SHARUN_REF="${QUICK_SHARUN_REF:-236785594d18d014f794351f7912e50ee23082eb}"

quick_sharun="${QUICK_SHARUN:-$(command -v quick-sharun || true)}"
if [ -z "$quick_sharun" ]; then
  mkdir -p "$work"
  quick_sharun="$work/quick-sharun"
  echo "fetching quick-sharun…"
  wget -q -O "$quick_sharun" \
    "https://raw.githubusercontent.com/pkgforge-dev/Anylinux-AppImages/${QUICK_SHARUN_REF}/useful-tools/quick-sharun.sh"
fi

"$PYTHON" -m pip install --upgrade pyinstaller
"$PYTHON" -m pip install -e .
"$PYTHON" packaging/make_icons.py

# Own work and dist paths, so the ordinary AppImage in dist/ is left alone.
rm -rf "$work/dist" "$work/work" "$appdir"
"$PYTHON" -m PyInstaller packaging/tjiptemp.spec --noconfirm \
  --distpath "$work/dist" --workpath "$work/work"
[ -d "$bundle" ] || { echo "PyInstaller did not produce $bundle" >&2; exit 1; }

# The GTK3 platform theme is the only thing in the bundle that links GTK, and the
# app sets the Fusion style regardless. Keeping it would drag all of GTK in.
rm -f "$bundle/_internal/PySide6/Qt/plugins/platformthemes/libqgtk3.so"

# PyInstaller finds _internal/ next to its executable, so the bundle goes into
# shared/bin/ as it is. sharun gets only the host libraries the bundle links
# against: handing it _internal as well copies all of it a second time into lib/.
mkdir -p "$appdir/shared/bin"
cp -a "$bundle/." "$appdir/shared/bin/"

ldd_out="$(find "$bundle" -type f \( -name '*.so*' -o -name tjiptemp \) -exec ldd {} + 2>/dev/null || true)"
if printf '%s\n' "$ldd_out" | grep -q 'not found'; then
  echo "the bundle links against libraries this host does not have:" >&2
  printf '%s\n' "$ldd_out" | grep 'not found' | sort -u >&2
  exit 1
fi
mapfile -t host_libs < <(printf '%s\n' "$ldd_out" \
  | awk '$2 == "=>" && $3 ~ /^\// {print $3}' | grep -v "^$root/$bundle/" | sort -u)
echo "deploying ${#host_libs[@]} host libraries with sharun"

cp packaging/icon-256.png "$work/io.github.tjiptemp.png"

# No update information: nothing publishes the .zsync that AppImageUpdate would
# need, and outside GitHub quick-sharun only guesses at it.
deploy() {
  env -u GITHUB_REPOSITORY \
    APPDIR="$appdir" OUTPATH="$root/$work/out" OUTNAME="$outname" VERSION="$version" \
    DESKTOP="$root/packaging/linux/tjiptemp.desktop" \
    ICON="$root/$work/io.github.tjiptemp.png" \
    STRACE_MODE=0 DEPLOY_QT=0 \
    sh "$quick_sharun" "$@"
}

rm -rf "$work/out"
deploy "$bundle/tjiptemp" "${host_libs[@]}"
deploy --make-appimage

mkdir -p dist
mv -f "$work/out/$outname" "dist/$outname"
echo "built: dist/$outname"
