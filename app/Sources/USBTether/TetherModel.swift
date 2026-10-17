import AppKit
import Foundation
import SwiftUI

/// What the UI shows. Derived from the status file the background service writes.
enum TetherState: Equatable {
    case notInstalled
    case serviceDown
    case disabled
    case waiting(phoneSeen: Bool)
    case connecting
    case reconnecting
    case connected
    case failed(String)
}

/// status.json, written by usbtether.py every couple of seconds.
struct StatusFile: Decodable {
    var state: String
    var detail: String?
    var time: Double
    var version: String?
    var ip: String?
    var iface: String?
    var rx_bytes: Int64?
    var tx_bytes: Int64?
    var since: Double?
    var glitches: Int?
    var reconnects: Int?
    var dropped: Int?
    var phone_seen: Bool?
}

struct LinkInfo: Equatable {
    var ip = ""
    var iface = ""
    var rxBytes: Int64 = 0
    var txBytes: Int64 = 0
    var reconnects = 0
    var glitches = 0
    var dropped = 0
}

final class TetherModel: ObservableObject {
    // MARK: published state

    @Published private(set) var state: TetherState = .serviceDown
    @Published private(set) var link = LinkInfo()
    @Published private(set) var downSpeed: Double = 0
    @Published private(set) var upSpeed: Double = 0
    @Published private(set) var downHistory: [Double] = []
    @Published private(set) var upHistory: [Double] = []
    @Published private(set) var uptime: TimeInterval = 0
    @Published private(set) var installedVersion: String?
    @Published private(set) var enabled = true
    @Published var busy = false
    @Published var busyText = ""
    @Published var errorText: String?
    @Published var service = ServiceSettings()

    let prefs = AppPrefs()
    let bundledVersion = ServiceInstaller.bundledVersion()

    private var timer: Timer?
    private var lastSample: (time: Double, rx: Int64, tx: Int64)?
    private let historyLength = 60

    // MARK: lifecycle

    init() {
        service = ServiceSettings.load()
        if prefs.notificationsEnabled { Notifier.shared.requestAuthorization() }
        refresh()

        timer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            self?.tick()
        }

        // After sleep the USB link is usually dead; ask the service to reconnect right away
        // instead of waiting for it to notice.
        NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didWakeNotification, object: nil, queue: .main
        ) { [weak self] _ in
            DispatchQueue.main.asyncAfter(deadline: .now() + 3) { self?.afterWake() }
        }
    }

    deinit {
        timer?.invalidate()
    }

    // MARK: derived values

    /// The service is installed but older (or newer) than the one bundled in this app.
    var serviceNeedsUpdate: Bool {
        switch state {
        case .notInstalled, .serviceDown:
            return false
        default:
            return installedVersion != bundledVersion
        }
    }

    var menuSymbol: String {
        switch state {
        case .connected: return "arrow.up.arrow.down.circle.fill"
        case .connecting, .reconnecting: return "arrow.triangle.2.circlepath.circle"
        case .waiting: return "iphone"
        case .disabled: return "pause.circle"
        case .failed: return "exclamationmark.triangle.fill"
        case .notInstalled, .serviceDown: return "exclamationmark.circle"
        }
    }

    var statusTitle: String {
        switch state {
        case .notInstalled: return "Set up USB Tether"
        case .serviceDown: return "Service not running"
        case .disabled: return "Turned off"
        case .waiting(let phoneSeen): return phoneSeen ? "Turn on USB tethering" : "Waiting for your phone"
        case .connecting: return "Connecting..."
        case .reconnecting: return "Reconnecting..."
        case .connected: return "Connected"
        case .failed: return "Couldn't connect"
        }
    }

    var statusSubtitle: String {
        switch state {
        case .notInstalled: return "One-time setup needed."
        case .serviceDown: return "The background service isn't responding."
        case .disabled: return "Auto-connect is off."
        case .waiting(let phoneSeen):
            return phoneSeen ? "Phone detected. Switch it on in the phone's settings." : "Plug it in with a USB cable."
        case .connecting: return "Setting up the link to your phone."
        case .reconnecting: return "The link dropped. Keeping your network settings meanwhile."
        case .connected: return "Using your phone's mobile data."
        case .failed(let detail): return detail
        }
    }

    // MARK: polling

    private func tick() {
        refresh()
        if state == .connected {
            downHistory.append(downSpeed)
            upHistory.append(upSpeed)
            if downHistory.count > historyLength { downHistory.removeFirst(downHistory.count - historyLength) }
            if upHistory.count > historyLength { upHistory.removeFirst(upHistory.count - historyLength) }
        } else if !downHistory.isEmpty {
            downHistory = []
            upHistory = []
        }
    }

    func refresh() {
        let fm = FileManager.default
        enabled = fm.fileExists(atPath: Paths.enabled)

        guard fm.fileExists(atPath: Paths.daemonPlist) else {
            installedVersion = nil
            resetLink()
            setState(.notInstalled)
            return
        }
        // Setting up a link can take ~30 s between status updates; launchd restarts a dead service.
        guard let st = readStatus(), Date().timeIntervalSince1970 - st.time < 45 else {
            installedVersion = nil
            resetLink()
            setState(.serviceDown)
            return
        }
        installedVersion = st.version

        switch st.state {
        case "connected":
            link = LinkInfo(ip: st.ip ?? "", iface: st.iface ?? "",
                            rxBytes: st.rx_bytes ?? 0, txBytes: st.tx_bytes ?? 0,
                            reconnects: st.reconnects ?? 0, glitches: st.glitches ?? 0,
                            dropped: st.dropped ?? 0)
            sampleSpeeds(time: st.time, rx: link.rxBytes, tx: link.txBytes)
            if let since = st.since { uptime = Date().timeIntervalSince1970 - since }
            setState(.connected)
        case "reconnecting":
            resetLink()
            setState(.reconnecting)
        case "connecting":
            resetLink()
            setState(.connecting)
        case "waiting":
            resetLink()
            setState(.waiting(phoneSeen: st.phone_seen ?? false))
        case "disabled":
            resetLink()
            setState(.disabled)
        case "error":
            resetLink()
            setState(.failed(st.detail ?? "Unknown error"))
        default:
            resetLink()
            setState(.serviceDown)
        }
    }

    private func readStatus() -> StatusFile? {
        guard let data = FileManager.default.contents(atPath: Paths.status) else { return nil }
        return try? JSONDecoder().decode(StatusFile.self, from: data)
    }

    private func sampleSpeeds(time: Double, rx: Int64, tx: Int64) {
        if let last = lastSample, rx >= last.rx, tx >= last.tx {
            let dt = time - last.time
            if dt > 0.2 {
                downSpeed = Double(rx - last.rx) / dt
                upSpeed = Double(tx - last.tx) / dt
                lastSample = (time, rx, tx)
            }
        } else {
            downSpeed = 0
            upSpeed = 0
            lastSample = (time, rx, tx)
        }
    }

    private func resetLink() {
        if link != LinkInfo() { link = LinkInfo() }
        downSpeed = 0
        upSpeed = 0
        uptime = 0
        lastSample = nil
    }

    private func setState(_ new: TetherState) {
        let old = state
        if old == new { return }
        state = new
        notifyIfNeeded(from: old, to: new)
    }

    private func notifyIfNeeded(from old: TetherState, to new: TetherState) {
        guard prefs.notificationsEnabled else { return }
        switch (old, new) {
        case (.reconnecting, .connected):
            break   // a brief blip that healed: no need to bother anyone
        case (_, .connected):
            Notifier.shared.post("Connected", "This Mac is now using your phone's internet.")
        case (.connected, .waiting), (.connected, .disabled), (.connected, .serviceDown),
             (.reconnecting, .waiting), (.reconnecting, .disabled), (.reconnecting, .serviceDown):
            Notifier.shared.post("Disconnected", "USB tethering stopped.")
        default:
            break
        }
    }

    // MARK: actions

    func setEnabled(_ on: Bool) {
        let fm = FileManager.default
        if on {
            if !fm.fileExists(atPath: Paths.enabled) {
                _ = fm.createFile(atPath: Paths.enabled, contents: nil, attributes: [.posixPermissions: 0o644])
            }
        } else {
            try? fm.removeItem(atPath: Paths.enabled)
        }
        enabled = fm.fileExists(atPath: Paths.enabled)
        if enabled != on {
            errorText = "Could not change the setting. Try Repair in Settings > Service."
        }
    }

    func requestReconnect() {
        _ = FileManager.default.createFile(atPath: Paths.reconnect, contents: nil,
                                           attributes: [.posixPermissions: 0o644])
    }

    private func afterWake() {
        guard enabled else { return }
        if state == .connected || state == .reconnecting {
            requestReconnect()
        }
    }

    /// Saves the service settings; reconnects if currently connected so they take effect.
    func applyServiceSettings() {
        do {
            try service.save()
            errorText = nil
        } catch {
            errorText = "Could not save settings: \(error.localizedDescription)"
            return
        }
        if state == .connected { requestReconnect() }
    }

    func installOrRepairService() {
        runInstaller(script: "setup.sh", withUser: true, text: "Setting up the background service...")
    }

    func removeService() {
        runInstaller(script: "remove.sh", withUser: false, text: "Removing the background service...")
    }

    private func runInstaller(script: String, withUser: Bool, text: String) {
        guard !busy else { return }
        busy = true
        busyText = text
        errorText = nil
        ServiceInstaller.run(script: script, withUser: withUser) { [weak self] result in
            guard let self = self else { return }
            self.busy = false
            if case .failure(let message) = result { self.errorText = message }
            self.service = ServiceSettings.load()
            self.refresh()
        }
    }
}
