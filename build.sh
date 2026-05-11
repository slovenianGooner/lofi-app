#!/usr/bin/env bash
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO"

echo "Generating icon…"
python3.13 make_icon.py

echo "Building lofi.app with py2app…"
rm -rf build dist
python3.13 setup.py py2app --no-strip 2>&1 | grep -Ev "^running|^creating|^copying|^making|byte-compiling"

echo ""
read -rp "Install to /Applications? [y/N] " answer
if [[ "$answer" =~ ^[Yy]$ ]]; then
    rm -rf /Applications/lofi.app
    cp -r dist/lofi.app /Applications/lofi.app
    echo "Installed to /Applications. Run with: open /Applications/lofi.app"
else
    echo "Done. App is at dist/lofi.app"
fi
