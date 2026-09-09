#!/usr/bin/env bash
# Cut a release: check, tag, push, and let CI build the three artifacts.
#
#     scripts/release.sh 0.2.0          tag v0.2.0 and push it
#     scripts/release.sh 0.2.0 --dry-run    say what would happen, change nothing
#     scripts/release.sh --local            build the three artifacts here, no tag
#
# The tag is the trigger: pushing v<version> starts the package job in
# .github/workflows/ci.yml, which builds the AppImage on Ubuntu, the .dmg on
# macOS and the .exe on Windows, and uploads all three. Nothing is built here
# unless you ask for --local, because only the matching runner can build for
# its own platform — a .dmg cannot be made on Linux.
#
# Refuses to tag a tree that is dirty, untested, unlinted, or whose version does
# not match pyproject.toml. A release is the one commit that gets copied to other
# people's machines, so it is worth the extra minute.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"

DRY_RUN=0
LOCAL_ONLY=0
VERSION=""
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        --local)   LOCAL_ONLY=1 ;;
        -h|--help) awk 'NR>1 && /^#/ {sub(/^# ?/,""); print; next} NR>1 {exit}' "$0"; exit 0 ;;
        -*)        echo "unknown option: $arg" >&2; exit 2 ;;
        *)         VERSION="$arg" ;;
    esac
done

# Same interpreter rule as the build scripts: an activated venv, then the
# project's own, then whatever python3 means.
if [ -z "${PYTHON:-}" ]; then
    if [ -n "${VIRTUAL_ENV:-}" ] && [ -x "$VIRTUAL_ENV/bin/python" ]; then
        PYTHON="$VIRTUAL_ENV/bin/python"
    elif [ -x "$root/.venv/bin/python" ]; then
        PYTHON="$root/.venv/bin/python"
    else
        PYTHON="$(command -v python3)"
    fi
fi

pyproject_version() {
    "$PYTHON" -c 'import tomllib,pathlib;print(tomllib.loads(pathlib.Path("pyproject.toml").read_text())["project"]["version"])'
}

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
fail() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
run()  { if [ "$DRY_RUN" = 1 ]; then printf '   would run: %s\n' "$*"; else "$@"; fi; }

# --------------------------------------------------------------- local build
if [ "$LOCAL_ONLY" = 1 ]; then
    step "Building locally for $(uname -s)"
    case "$(uname -s)" in
        Linux)  ./packaging/linux/build_appimage.sh ;;
        Darwin) ./packaging/macos/build.sh ;;
        *)      fail "on Windows run: powershell -ExecutionPolicy Bypass -File packaging\\windows\\build.ps1" ;;
    esac
    echo
    ls -lh dist/*.AppImage dist/*.dmg dist/*.exe 2>/dev/null || true
    echo
    echo "Only this platform's artifact was built. The other two need their own"
    echo "runner — push a tag and let CI do all three."
    exit 0
fi

[ -n "$VERSION" ] || fail "no version given. Usage: scripts/release.sh 0.2.0 [--dry-run]"
case "$VERSION" in
    [0-9]*.[0-9]*.[0-9]*) ;;
    *) fail "version must look like 1.2.3, got '$VERSION'" ;;
esac

tag="v$VERSION"

# ------------------------------------------------------------------- checks
step "Checking the working tree"
[ -z "$(git status --porcelain)" ] || fail "working tree is dirty; commit or stash first"

current="$(pyproject_version)"
[ "$current" = "$VERSION" ] || fail "pyproject.toml says $current, you asked for $VERSION.
       Bump the version, commit it, then run this again."

! git rev-parse -q --verify "refs/tags/$tag" >/dev/null \
    || fail "tag $tag already exists. Releases are not reissued under the same
       version — bump to the next one instead."

branch="$(git rev-parse --abbrev-ref HEAD)"
if [ "$branch" != "master" ] && [ "$branch" != "main" ]; then
    printf 'you are on "%s", not master. Continue? [y/N] ' "$branch"
    read -r reply
    case "$reply" in [Yy]*) ;; *) fail "stopped" ;; esac
fi

if git remote get-url origin >/dev/null 2>&1; then
    git fetch --quiet origin "$branch" 2>/dev/null || true
    behind="$(git rev-list --count "HEAD..origin/$branch" 2>/dev/null || echo 0)"
    [ "$behind" = "0" ] || fail "your branch is $behind commit(s) behind origin/$branch; pull first"
fi

step "Linting"
"$PYTHON" -m ruff check src tests packaging || fail "ruff is unhappy"

step "Running the tests"
QT_QPA_PLATFORM=offscreen "$PYTHON" -m pytest tests/ -q || fail "tests failed"

step "Cross-checking the C and Python codecs"
# The check that keeps docs/protocol.md honest: if these two disagree, the
# firmware and the desktop disagree on the wire, and no test above would say so.
(cd firmware-ref && make --quiet test >/dev/null && ./test_tjip_proto --vectors) > /tmp/tjip-c-vectors.txt
PYTHONPATH=src "$PYTHON" -m tests.crosscheck_vectors > /tmp/tjip-py-vectors.txt
diff -u /tmp/tjip-c-vectors.txt /tmp/tjip-py-vectors.txt \
    || fail "the C and Python codecs disagree — do not release this"
echo "   codecs agree byte-for-byte"

step "Checking the bundle can actually be built"
# The spec has failed before on things a test suite cannot see: a missing icon,
# an entry point whose relative imports do not resolve once frozen. Generating
# the icons and importing the launcher catches both in a second.
"$PYTHON" packaging/make_icons.py >/dev/null
for f in packaging/icon.png packaging/icon.ico packaging/icon.icns; do
    [ -s "$f" ] || fail "$f was not generated"
done
PYTHONPATH=src "$PYTHON" -c 'import importlib.util,pathlib,sys
spec = importlib.util.spec_from_file_location("_probe", "packaging/entrypoint.py")
module = importlib.util.module_from_spec(spec)
sys.argv = ["tjiptemp", "--version"]
try:
    spec.loader.exec_module(module)
except SystemExit:
    pass' >/dev/null || fail "packaging/entrypoint.py does not import cleanly"
echo "   icons generate, entry point imports"

# --------------------------------------------------------------------- tag
step "Tagging $tag"
run git tag -a "$tag" -m "$VERSION"

if git remote get-url origin >/dev/null 2>&1; then
    step "Pushing $branch and $tag"
    run git push origin "$branch"
    run git push origin "$tag"
else
    printf '\nno "origin" remote; tagged locally only.\n'
fi

if [ "$DRY_RUN" = 1 ]; then
    printf '\n\033[1mdry run: nothing was tagged or pushed.\033[0m\n'
    exit 0
fi

url="$(git remote get-url origin 2>/dev/null | sed -e 's#git@github.com:#https://github.com/#' -e 's#\.git$##')"
cat <<DONE

Released $tag.

CI is now building all three artifacts. Watch it at:
  ${url:-<no remote>}/actions

When it finishes, the AppImage, .dmg and .exe are attached to that run.
Draft the release notes at:
  ${url:-<no remote>}/releases/new?tag=$tag

Neither the .dmg nor the .exe is code-signed, so macOS asks for a
right-click > Open the first time and Windows SmartScreen warns until the
binary earns a reputation. Signing needs an Apple Developer ID and a
Windows code-signing certificate; the build scripts pick them up from
SIGN_ID / NOTARY_PROFILE and SIGN_CERT / SIGN_PASS when you have them.
DONE
