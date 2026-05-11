#!/usr/bin/env bash
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="$REPO/lofi.app"

echo "Building lofi.app…"

mkdir -p "$APP/Contents/MacOS"
mkdir -p "$APP/Contents/Resources"

cp "$REPO/lofi.py" "$APP/Contents/Resources/"
cp "$REPO/Info.plist"       "$APP/Contents/"

cat > "$APP/Contents/MacOS/lofi" << 'EOF'
#!/usr/bin/env bash
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

RESOURCES="$(cd "$(dirname "$0")/../Resources" && pwd)"

python3.13 "$RESOURCES/lofi.py" &
PYTHON_PID=$!

cleanup() { kill "$PYTHON_PID" 2>/dev/null; wait "$PYTHON_PID" 2>/dev/null; true; }
trap cleanup EXIT TERM INT

wait "$PYTHON_PID"
EOF
chmod +x "$APP/Contents/MacOS/lofi"

if [ -d "$REPO/lofi.iconset" ]; then
    iconutil -c icns "$REPO/lofi.iconset" -o "$APP/Contents/Resources/lofi.icns"
    echo "Icon generated."
fi

echo ""
read -rp "Install to /Applications? [y/N] " answer
if [[ "$answer" =~ ^[Yy]$ ]]; then
    cp -r "$APP" /Applications/lofi.app
    echo "Installed to /Applications. Run with: open /Applications/lofi.app"
else
    echo "Done. Run with: open lofi.app"
fi
