#!/usr/bin/env bash
# Removes USB Tether completely: the app, the background service and any older versions.
# (If "Open at login" was on, switch it off in the app's Settings first, or remove the
# item in System Settings > General > Login Items.)
set -uo pipefail

UID_NUM="$(id -u)"
DAEMON_PLIST=/Library/LaunchDaemons/com.usbtether.daemon.plist
OLD_AGENT="$HOME/Library/LaunchAgents/com.usbtether.menubar.plist"

pkill -x USBTether 2>/dev/null
launchctl bootout "gui/$UID_NUM" "$OLD_AGENT" 2>/dev/null
rm -f "$OLD_AGENT"
rm -rf "$HOME/Library/Application Support/usbtether"

# Stopping the service lets it restore the Mac's routes and DNS before it exits.
sudo launchctl bootout system "$DAEMON_PLIST" 2>/dev/null
sleep 1
sudo rm -f "$DAEMON_PLIST" /etc/newsyslog.d/usbtether.conf
sudo rm -rf /usr/local/libexec/usbtether /Users/Shared/usbtether
rm -rf "/Applications/USB Tether.app"

echo "USB Tether removed."
