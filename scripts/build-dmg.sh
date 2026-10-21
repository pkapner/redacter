#!/bin/sh
# Build dist/Redacter.dmg: a self-contained Redacter.app (Python and all
# libraries inside) on a disk image with a drag-to-Applications layout.
# Run on a Mac; takes a few minutes the first time.
set -e
[ "$(uname)" = Darwin ] || { echo "build-dmg.sh must run on macOS" >&2; exit 1; }
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

PY=""
for p in python3.13 python3.12 python3.11 python3; do
    if command -v "$p" >/dev/null && "$p" -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
        PY="$p"; break
    fi
done
[ -n "$PY" ] || { echo "Need Python 3.11+ (brew install python@3.12)" >&2; exit 1; }

# A separate, clean environment keeps the bundle free of dev tools.
echo "==> preparing build environment ($PY)"
"$PY" -m venv .build-venv
.build-venv/bin/pip install -q --upgrade pip
.build-venv/bin/pip install -q '.[gui]' pyinstaller

echo "==> building Redacter.app"
rm -rf build dist
.build-venv/bin/pyinstaller --noconfirm --clean --log-level WARN \
    --distpath dist --workpath build packaging/redacter.spec
APP=dist/Redacter.app

# Ad-hoc signature: required for the app to launch on Apple Silicon.
codesign --force --deep --sign - "$APP"

echo "==> checking the app works"
"$APP/Contents/MacOS/Redacter" --selftest

echo "==> making disk image"
STAGE=dist/dmg
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
hdiutil create -quiet -volname Redacter -srcfolder "$STAGE" -ov -format UDZO dist/Redacter.dmg
rm -rf "$STAGE"

echo "Built $REPO/dist/Redacter.dmg ($(du -h dist/Redacter.dmg | cut -f1))"
