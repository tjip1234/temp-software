#!/usr/bin/env bash
# Build TjipTemp.app and a .dmg.
#
#     ./packaging/macos/build.sh                 unsigned, fine for your own machine
#     SIGN_ID="Developer ID Application: ..." \
#     NOTARY_PROFILE=tjiptemp ./packaging/macos/build.sh    signed and notarised
#
# Without signing, macOS Gatekeeper will refuse to open the app on any machine
# that did not build it; users can right-click > Open once to bypass. For actual
# distribution you need an Apple Developer ID and notarisation, hence the two
# environment variables above.
set -euo pipefail

root="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$root"


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

"$PYTHON" -m pip install --upgrade pyinstaller
"$PYTHON" -m pip install -e .

# The spec refuses to embed an icon it cannot open, so generate them first.
"$PYTHON" packaging/make_icons.py

rm -rf build dist
"$PYTHON" -m PyInstaller packaging/tjiptemp.spec --noconfirm

app="dist/TjipTemp.app"
[ -d "$app" ] || { echo "PyInstaller did not produce $app" >&2; exit 1; }

if [ -n "${SIGN_ID:-}" ]; then
  echo "signing…"
  # --deep is deprecated but still the pragmatic option for a PyInstaller tree
  # full of nested dylibs; the hardened runtime is what notarisation requires.
  codesign --force --deep --options runtime --timestamp \
           --sign "$SIGN_ID" "$app"
  codesign --verify --strict --verbose=2 "$app"
fi

dmg="dist/TjipTemp-$("$PYTHON" -c 'import tomllib,pathlib;print(tomllib.loads(pathlib.Path("pyproject.toml").read_text())["project"]["version"])').dmg"
rm -f "$dmg"
hdiutil create -volname TjipTemp -srcfolder "$app" -ov -format UDZO "$dmg"

if [ -n "${SIGN_ID:-}" ]; then
  codesign --force --sign "$SIGN_ID" "$dmg"
fi

if [ -n "${NOTARY_PROFILE:-}" ]; then
  echo "notarising… (this takes a few minutes)"
  xcrun notarytool submit "$dmg" --keychain-profile "$NOTARY_PROFILE" --wait
  xcrun stapler staple "$dmg"
  xcrun stapler validate "$dmg"
fi

echo "built: $dmg"
