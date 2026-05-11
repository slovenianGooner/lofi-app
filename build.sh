#!/usr/bin/env bash
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO"

RELEASE=false
for arg in "$@"; do
    [[ "$arg" == "--release" ]] && RELEASE=true
done

echo "Generating icon…"
python3.13 make_icon.py

echo "Building lofi.app with py2app…"
rm -rf build dist
python3.13 setup.py py2app --no-strip 2>&1 | grep -Ev "^running |^creating |^copying |^making |byte-compiling|^---"

if $RELEASE; then
    VERSION=$(python3.13 -c "
import re, pathlib
m = re.search(r'\"CFBundleShortVersionString\":\s*\"([^\"]+)\"', pathlib.Path('setup.py').read_text())
print(m.group(1) if m else '1.0')
")
    TAG="v$VERSION"
    ZIP="lofi-${TAG}-macos.zip"

    echo "Zipping…"
    (cd dist && zip -qr "../$ZIP" lofi.app)
    echo "  → $ZIP ($(du -sh "$ZIP" | cut -f1))"

    echo "Publishing GitHub release $TAG…"
    gh release create "$TAG" "$ZIP" \
        --title "lofi $TAG" \
        --notes "Self-contained macOS app. No dependencies required — yt-dlp is bundled." \
        --latest

    rm -f "$ZIP"
    REPO_SLUG=$(gh repo view --json nameWithOwner -q .nameWithOwner)
    echo "Released: https://github.com/$REPO_SLUG/releases/tag/$TAG"
else
    echo ""
    read -rp "Install to /Applications? [y/N] " answer
    if [[ "$answer" =~ ^[Yy]$ ]]; then
        rm -rf /Applications/lofi.app
        cp -r dist/lofi.app /Applications/lofi.app
        echo "Installed to /Applications. Run with: open /Applications/lofi.app"
    else
        echo "Done. App is at dist/lofi.app"
    fi
fi
