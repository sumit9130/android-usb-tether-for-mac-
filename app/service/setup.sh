#!/bin/bash
# Installs or repairs the USB Tether background service. Run as root by the app (through the
# macOS administrator password prompt). Safe to run again; it keeps your on/off setting.
#   setup.sh <folder containing usbtether.py and VERSION> <login name>
set -euo pipefail

SRC="${1:?missing source folder}"
USER_NAME="${2:?missing user name}"
SVC=/usr/local/libexec/usbtether
PLIST=/Library/LaunchDaemons/com.usbtether.daemon.plist
CTRL=/Users/Shared/usbtether

[ "$(id -u)" -eq 0 ] || { echo "setup.sh must run as root"; exit 1; }
[ -f "$SRC/usbtether.py" ] || { echo "usbtether.py not found in $SRC"; exit 1; }

# --- a working Python 3 --------------------------------------------------------------
PY=""
for c in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    if [ -x "$c" ] && "$c" -c 'import venv, ssl' >/dev/null 2>&1; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
    echo "Python 3 was not found. Open Terminal, run 'xcode-select --install', let it finish, then try again."
    exit 2
fi

# --- program files (root-owned, so the root service never runs user-editable code) ------
mkdir -p "$SVC"
cp "$SRC/usbtether.py" "$SVC/usbtether.py"
[ -f "$SRC/VERSION" ] && cp "$SRC/VERSION" "$SVC/VERSION"
chown -R root:wheel "$SVC"
chmod 755 "$SVC"

if [ ! -x "$SVC/venv/bin/python" ]; then
    "$PY" -m venv "$SVC/venv"
fi
if ! "$SVC/venv/bin/python" -m pip install -q --disable-pip-version-check pyusb; then
    echo "Could not download the pyusb package. Check your internet connection and try again."
    exit 4
fi
# A bundled libusb, so Homebrew isn't needed. Optional: Homebrew's libusb is the fallback.
"$SVC/venv/bin/python" -m pip install -q --disable-pip-version-check libusb-package \
    || echo "note: could not install libusb-package; relying on Homebrew libusb"

if ! "$SVC/venv/bin/python" "$SVC/usbtether.py" --check >/tmp/usbtether-check.txt 2>&1; then
    cat /tmp/usbtether-check.txt
    echo "libusb could not be loaded. Open Terminal, run 'brew install libusb', then try again."
    exit 3
fi

# --- control folder shared with the app (owned by the user, not world-writable) ----------
mkdir -p "$CTRL"
chown "$USER_NAME" "$CTRL"
chmod 755 "$CTRL"
[ -e "$CTRL/enabled" ] || touch "$CTRL/enabled"
[ -e "$CTRL/settings.json" ] || echo '{"route_all": true, "dns": "", "mtu": 1500}' > "$CTRL/settings.json"
rm -f "$CTRL/reconnect"
chown "$USER_NAME" "$CTRL"/enabled "$CTRL"/settings.json
chmod 644 "$CTRL"/enabled "$CTRL"/settings.json

# --- log rotation (3 old copies, rotate at 512 KB) ---------------------------------------
echo '/var/log/usbtether.log 644 3 512 * J' > /etc/newsyslog.d/usbtether.conf

# --- launchd service ------------------------------------------------------------------------
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.usbtether.daemon</string>
    <key>ProgramArguments</key>
    <array>
        <string>$SVC/venv/bin/python</string>
        <string>-u</string>
        <string>$SVC/usbtether.py</string>
        <string>--daemon</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ThrottleInterval</key><integer>5</integer>
    <key>StandardOutPath</key><string>/var/log/usbtether.log</string>
    <key>StandardErrorPath</key><string>/var/log/usbtether.log</string>
    <key>EnvironmentVariables</key>
    <dict><key>PATH</key><string>/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin</string></dict>
</dict>
</plist>
PLISTEOF
chown root:wheel "$PLIST"
chmod 644 "$PLIST"
launchctl bootout system "$PLIST" 2>/dev/null || true
launchctl bootstrap system "$PLIST"

# --- remove the old Python menu bar version, if it was ever installed ----------------------
USER_UID="$(id -u "$USER_NAME" 2>/dev/null || true)"
HOME_DIR="$(dscl . -read "/Users/$USER_NAME" NFSHomeDirectory 2>/dev/null | awk '{print $2}')"
if [ -n "$USER_UID" ] && [ -n "$HOME_DIR" ]; then
    OLD="$HOME_DIR/Library/LaunchAgents/com.usbtether.menubar.plist"
    if [ -f "$OLD" ]; then
        launchctl bootout "gui/$USER_UID" "$OLD" 2>/dev/null || true
        rm -f "$OLD"
        rm -rf "$HOME_DIR/Library/Application Support/usbtether"
    fi
fi

echo "USB Tether service installed."
