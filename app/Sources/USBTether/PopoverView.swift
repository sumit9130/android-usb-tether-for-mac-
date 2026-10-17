import AppKit
import SwiftUI

/// The window that drops down from the menu bar icon.
struct PopoverView: View {
    @EnvironmentObject var model: TetherModel

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            VStack(alignment: .leading, spacing: 12) {
                if model.busy {
                    Banner(tint: .blue) {
                        ProgressView().controlSize(.small)
                        Text(model.busyText)
                    }
                }
                if let error = model.errorText {
                    Banner(tint: .red) {
                        Text(error)
                            .textSelection(.enabled)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                if model.serviceNeedsUpdate && !model.busy {
                    Banner(tint: .orange) {
                        Text("Background service update available.")
                        Spacer()
                        Button("Update") { model.installOrRepairService() }
                    }
                }
                content
            }
            .padding(14)
            if model.state != .notInstalled {
                Divider()
                footer
            }
        }
        .frame(width: 330)
    }

    // MARK: header

    private var header: some View {
        HStack(alignment: .top, spacing: 10) {
            StatusDot(state: model.state).padding(.top, 5)
            VStack(alignment: .leading, spacing: 2) {
                Text(model.statusTitle).font(.headline)
                Text(model.statusSubtitle)
                    .font(.caption)
                    .foregroundColor(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer()
        }
        .padding(14)
    }

    // MARK: body

    @ViewBuilder
    private var content: some View {
        switch model.state {
        case .notInstalled:
            setupCard
        case .connected:
            connectedCard
        case .waiting(let phoneSeen):
            waitingCard(phoneSeen: phoneSeen)
        case .failed:
            Text("Try unplugging the phone, switching USB tethering off and on, and plugging it back in. "
                 + "Settings > Service > Show Log has more detail.")
                .font(.caption)
                .foregroundColor(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        case .serviceDown:
            VStack(alignment: .leading, spacing: 8) {
                Text("The background service stopped or isn't responding.")
                    .font(.callout)
                Button("Repair Service") { model.installOrRepairService() }
                    .disabled(model.busy)
            }
        case .disabled:
            Text("Turn Auto-connect on to share your phone's connection whenever it is plugged in.")
                .font(.callout)
                .foregroundColor(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        case .connecting, .reconnecting:
            EmptyView()
        }
    }

    private var setupCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("USB Tether adds a small background service that builds the network link to your phone. "
                 + "It needs your administrator password once.")
                .font(.callout)
                .fixedSize(horizontal: false, vertical: true)
            VStack(alignment: .leading, spacing: 5) {
                bullet("Starts with your Mac")
                bullet("Connects whenever the phone is plugged in with USB tethering on")
                bullet("Restores your normal network when you unplug")
            }
            Button(action: { model.installOrRepairService() }) {
                Text(model.busy ? "Installing..." : "Install and Set Up").frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.large)
            .disabled(model.busy)
            Text("Needs Python 3 (included with Xcode's command line tools) and an internet connection for this first setup.")
                .font(.caption)
                .foregroundColor(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            HStack {
                Spacer()
                Button("Quit") { NSApplication.shared.terminate(nil) }
                    .buttonStyle(.link)
            }
        }
    }

    private func bullet(_ text: String) -> some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: "checkmark").foregroundColor(.green).font(.caption)
            Text(text).font(.callout)
        }
    }

    private func waitingCard(phoneSeen: Bool) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            step(done: phoneSeen, number: "1", text: "Plug the phone into this Mac with a USB cable.")
            step(done: false, number: "2",
                 text: "On the phone open Settings > Connections > Mobile Hotspot and Tethering, and switch on USB tethering.")
            Text("The Mac connects within a few seconds.")
                .font(.caption)
                .foregroundColor(.secondary)
        }
    }

    private func step(done: Bool, number: String, text: String) -> some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: done ? "checkmark.circle.fill" : "\(number).circle")
                .foregroundColor(done ? .green : .secondary)
            Text(text)
                .font(.callout)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private var connectedCard: some View {
        let peak = max(model.downHistory.max() ?? 0, model.upHistory.max() ?? 0)
        return VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 12) {
                SpeedBlock(symbol: "arrow.down", title: "Download",
                           value: formatSpeed(model.downSpeed), color: .blue)
                SpeedBlock(symbol: "arrow.up", title: "Upload",
                           value: formatSpeed(model.upSpeed), color: .orange)
            }
            ZStack {
                Sparkline(values: model.downHistory, maxValue: peak, color: .blue)
                Sparkline(values: model.upHistory, maxValue: peak, color: .orange)
            }
            .frame(height: 44)
            .padding(.vertical, 2)

            VStack(spacing: 5) {
                StatRow(label: "Connected for", value: formatDuration(model.uptime))
                StatRow(label: "Address", value: model.link.ip)
                StatRow(label: "Interface", value: model.link.iface)
                StatRow(label: "Received", value: formatBytes(model.link.rxBytes))
                StatRow(label: "Sent", value: formatBytes(model.link.txBytes))
                StatRow(label: "Stability", value: stabilityText)
            }
        }
    }

    private var stabilityText: String {
        let r = model.link.reconnects
        let g = model.link.glitches
        if r == 0 && g == 0 { return "Steady" }
        var parts: [String] = []
        if r > 0 { parts.append("\(r) reconnect\(r == 1 ? "" : "s")") }
        if g > 0 { parts.append("\(g) glitch\(g == 1 ? "" : "es") recovered") }
        return parts.joined(separator: ", ")
    }

    // MARK: footer

    private var footer: some View {
        HStack(spacing: 10) {
            Toggle("Auto-connect", isOn: Binding(
                get: { model.enabled },
                set: { model.setEnabled($0) }
            ))
            .toggleStyle(.switch)
            .controlSize(.small)

            Spacer()

            Button(action: { model.requestReconnect() }) {
                Image(systemName: "arrow.clockwise")
            }
            .buttonStyle(.borderless)
            .help("Reconnect now")
            .disabled(!(model.state == .connected || model.state == .reconnecting))

            Button(action: openSettings) {
                Image(systemName: "gearshape")
            }
            .buttonStyle(.borderless)
            .help("Settings")

            Button(action: { NSApplication.shared.terminate(nil) }) {
                Image(systemName: "power")
            }
            .buttonStyle(.borderless)
            .help("Quit USB Tether (the background service keeps running)")
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
    }

    private func openSettings() {
        SettingsWindowController.shared.show(model: model)
    }
}
