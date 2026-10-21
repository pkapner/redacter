#!/bin/sh
# Build Redacter.app: a double-clickable launcher for this checkout's app.
# Drop PDFs on its Dock icon, or use Open With > Redacter in Finder.
#
#   scripts/make-mac-app.sh            # installs to ~/Applications
#   scripts/make-mac-app.sh /Applications
set -e
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="$REPO/.venv/bin/python"
if ! "$PY" -c "import PySide6" 2>/dev/null; then
    echo "Install the app first:  cd $REPO && .venv/bin/pip install -e '.[gui]'" >&2
    exit 1
fi
DEST="${1:-$HOME/Applications}"
APP="$DEST/Redacter.app"
mkdir -p "$APP/Contents/MacOS"

cat > "$APP/Contents/MacOS/Redacter" <<EOF
#!/bin/sh
exec "$PY" -m redacter.gui "\$@"
EOF
chmod +x "$APP/Contents/MacOS/Redacter"

cat > "$APP/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>Redacter</string>
    <key>CFBundleDisplayName</key><string>Redacter</string>
    <key>CFBundleIdentifier</key><string>local.redacter</string>
    <key>CFBundleExecutable</key><string>Redacter</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleVersion</key><string>1</string>
    <key>NSHighResolutionCapable</key><true/>
    <key>CFBundleDocumentTypes</key>
    <array>
        <dict>
            <key>CFBundleTypeName</key><string>PDF document</string>
            <key>CFBundleTypeRole</key><string>Editor</string>
            <key>LSHandlerRank</key><string>Alternate</string>
            <key>LSItemContentTypes</key>
            <array><string>com.adobe.pdf</string></array>
        </dict>
    </array>
</dict>
</plist>
EOF
echo "Built $APP"
echo "Open it from Finder (or drag it to the Dock). It runs the code in $REPO."
