#!/usr/bin/env bash
# One-off launcher (no app needed): installs pyusb + a bundled libusb on first run,
# then starts the tether as root.   ./usbtether.sh [--list] [--verbose] [--no-default-route] [--dns 8.8.8.8]
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
    python3 -m venv .venv
    .venv/bin/pip install -q -r requirements.txt
fi

# libusb comes bundled with the libusb-package wheel; fall back to Homebrew if that failed.
if ! .venv/bin/python usbtether.py --check >/dev/null 2>&1; then
    command -v brew >/dev/null 2>&1 || { echo "libusb is missing. Install Homebrew (https://brew.sh), then re-run."; exit 1; }
    brew install libusb
fi

# adb holds the phone's USB device open, which can get in the way.
command -v adb >/dev/null 2>&1 && adb kill-server >/dev/null 2>&1 || true

exec sudo .venv/bin/python usbtether.py "$@"
