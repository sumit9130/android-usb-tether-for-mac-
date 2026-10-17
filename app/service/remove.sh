#!/bin/bash
# Removes the USB Tether background service. Run as root by the app.
set -uo pipefail

PLIST=/Library/LaunchDaemons/com.usbtether.daemon.plist

# Stopping the service lets it restore the Mac's routes and DNS before it exits.
launchctl bootout system "$PLIST" 2>/dev/null
sleep 1
rm -f "$PLIST" /etc/newsyslog.d/usbtether.conf
rm -rf /usr/local/libexec/usbtether /Users/Shared/usbtether
echo "USB Tether service removed."
