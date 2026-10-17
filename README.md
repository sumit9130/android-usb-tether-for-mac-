# USB Tether

Use your Android phone's mobile data on a Mac over USB. macOS has no driver for Android's RNDIS tethering; this app supplies one in userspace. No kernel extension, no phone app.

It is a menu bar app (no Dock icon) plus a small background service that does the networking.

## Install

Easiest: download `USB-Tether-<version>.dmg` from the Releases page (or the latest build under Actions > Artifacts), drag the app to Applications, and open it with right-click > Open the first time.

Or build it yourself:

Requires macOS 13 or later and Xcode's command line tools (`xcode-select --install`), which also provide Python 3.

```bash
cd usb-tether/app
./build_app.sh --install      # builds "USB Tether.app", copies it to /Applications, opens it
./make_dmg.sh                 # optional: USB-Tether-<version>.dmg to hand to another Mac
```

The app is ad-hoc signed. If macOS blocks the first launch, right-click the app, choose Open, then Open again.

## First run

1. Click the menu bar icon, then **Install and Set Up**. Enter your administrator password once. This needs an internet connection (it installs `pyusb` and `libusb` into a private environment).
2. On the phone: Settings > Connections > Mobile Hotspot and Tethering > turn on **USB tethering**.
3. Plug in the phone. The Mac connects within a few seconds.

After that the service starts with the Mac and connects by itself whenever the phone is plugged in. The app only shows status and settings; it can be quit without dropping the connection.

## Menu bar

- Status, download/upload speed with a graph, address, data totals, and a stability line (reconnects and recovered glitches).
- **Auto-connect** switch, **Reconnect now**, **Settings**, **Quit**.

## Settings

- **General**: open at login, notifications, live speed in the menu bar.
- **Network**: send all traffic through the phone, custom DNS, MTU (lower it to about 1400 if some sites hang).
- **Service**: installed and bundled versions, repair/update, remove, log viewer.
- **About**.

## Stability design

- USB errors are retried (with `clear_halt`) before being treated as failures.
- A watchdog probes the phone with ARP after 15 s of silence.
- If the phone drops and returns, the interface, routes and DNS are kept for about 20 s so connections survive a brief unplug.
- After repeated failures the USB device is reset.
- Your normal routes and DNS are restored when the phone is gone for good or the service stops.
- Every drop is logged with its cause in `/var/log/usbtether.log`.

## Command line

`./usbtether.sh` runs the driver in the foreground (needs sudo). Flags: `--list`, `--verbose`, `--no-default-route`, `--dns 1.1.1.1`, `--mtu 1400`, `--version`, `--check`.

## Uninstall

`./uninstall.sh` removes the app, the service and any older versions.

## Troubleshooting

- **Stays on "Waiting for phone"**: confirm USB tethering is on (it turns off when the cable is replugged on some phones) and try a data cable, not a charge-only one.
- **Setup fails**: the message names the cause (no Python 3, libusb missing, or package download failed). Fix it and press Install again.
- **Drops**: open Settings > Service > Show Log and look at lines around "USB link lost" or "USB glitch".
- **Tests**: `python3 test_protocol.py` (protocol and resilience, simulated phone).

## Status

The Python service is covered by 36 automated tests with a simulated phone, and the earlier version was verified on a real Mac and Galaxy S21 FE. The Swift app and the newer resilience logic were written without a Swift compiler or real hardware, so a first build may need a small fix. If `build_app.sh` prints errors, send them over.
