#!/usr/bin/env bash
# Builds "USB Tether.app" next to this script. Needs Xcode (or its command line tools).
#   ./build_app.sh             build
#   ./build_app.sh --install   build, copy to /Applications and open it
set -euo pipefail
cd "$(dirname "$0")"

command -v swift >/dev/null 2>&1 || { echo "Swift not found. Install Xcode, or run: xcode-select --install"; exit 1; }
VERSION="$(tr -d '[:space:]' < ../VERSION)"

echo "==> Compiling USB Tether $VERSION"
if swift build -c release --arch arm64 --arch x86_64 2>/dev/null; then
    BIN="$(swift build -c release --arch arm64 --arch x86_64 --show-bin-path)/USBTether"
    echo "    (universal binary)"
else
    swift build -c release
    BIN="$(swift build -c release --show-bin-path)/USBTether"
fi

APP="USB Tether.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/service"
cp "$BIN" "$APP/Contents/MacOS/USBTether"

# the files the in-app installer puts in place (root-owned copy of the service)
cp ../usbtether.py ../VERSION service/setup.sh service/remove.sh "$APP/Contents/Resources/service/"
chmod +x "$APP/Contents/Resources/service/"*.sh

# icon
if [ -f AppIcon.png ]; then
    ICONSET="$(mktemp -d)/AppIcon.iconset"
    mkdir -p "$ICONSET"
    for s in 16 32 128 256 512; do
        sips -z $s $s AppIcon.png --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
        sips -z $((s*2)) $((s*2)) AppIcon.png --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
    done
    iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"
fi

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>USB Tether</string>
    <key>CFBundleDisplayName</key><string>USB Tether</string>
    <key>CFBundleIdentifier</key><string>com.usbtether.app</string>
    <key>CFBundleExecutable</key><string>USBTether</string>
    <key>CFBundleIconFile</key><string>AppIcon</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>$VERSION</string>
    <key>CFBundleVersion</key><string>$VERSION</string>
    <key>LSMinimumSystemVersion</key><string>13.0</string>
    <key>LSUIElement</key><true/>
    <key>LSMultipleInstancesProhibited</key><true/>
    <key>LSApplicationCategoryType</key><string>public.app-category.utilities</string>
    <key>NSHighResolutionCapable</key><true/>
    <key>NSPrincipalClass</key><string>NSApplication</string>
    <key>NSHumanReadableCopyright</key><string>USB Tether $VERSION</string>
</dict>
</plist>
PLIST

# ad-hoc signature so macOS treats it as a proper app (login item, notifications)
codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || echo "(codesign skipped)"

echo "==> Built: $(pwd)/$APP"
if [ "${1:-}" = "--install" ]; then
    pkill -x USBTether 2>/dev/null || true
    rm -rf "/Applications/$APP"
    cp -R "$APP" /Applications/
    echo "==> Installed to /Applications/$APP"
    open "/Applications/$APP"
fi
