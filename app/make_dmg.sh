#!/usr/bin/env bash
# Packs "USB Tether.app" into a drag-to-Applications disk image: USB-Tether-<version>.dmg
set -euo pipefail
cd "$(dirname "$0")"

[ -d "USB Tether.app" ] || ./build_app.sh
VERSION="$(tr -d '[:space:]' < ../VERSION)"
STAGE="$(mktemp -d)"
cp -R "USB Tether.app" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
OUT="USB-Tether-$VERSION.dmg"
rm -f "$OUT"
hdiutil create -volname "USB Tether" -srcfolder "$STAGE" -ov -format UDZO "$OUT" >/dev/null
rm -rf "$STAGE"
echo "Created $(pwd)/$OUT"
