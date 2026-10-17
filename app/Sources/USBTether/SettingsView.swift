import AppKit
import SwiftUI

struct SettingsView: View {
    @EnvironmentObject var model: TetherModel
    @EnvironmentObject var prefs: AppPrefs

    var body: some View {
        TabView {
            GeneralTab()
                .tabItem { Label("General", systemImage: "gearshape") }
            NetworkTab()
                .tabItem { Label("Network", systemImage: "network") }
            ServiceTab()
                .tabItem { Label("Service", systemImage: "wrench.and.screwdriver") }
            AboutTab()
                .tabItem { Label("About", systemImage: "info.circle") }
        }
        .frame(width: 500, height: 400)
    }
}

// MARK: - General

private struct GeneralTab: View {
    @EnvironmentObject var model: TetherModel
    @EnvironmentObject var prefs: AppPrefs
    @State private var loginError: String?

    var body: some View {
        Form {
            Section {
                Toggle("Open USB Tether when I log in", isOn: Binding(
                    get: { prefs.launchAtLogin },
                    set: { loginError = prefs.setLaunchAtLogin($0) }
                ))
                Toggle("Show notifications when the link connects or drops", isOn: $prefs.notificationsEnabled)
                Toggle("Show live speed in the menu bar", isOn: $prefs.showSpeedInMenuBar)
                if let loginError = loginError {
                    Text(loginError).font(.caption).foregroundColor(.red)
                }
            }
            Section {
                Toggle("Auto-connect when the phone is plugged in", isOn: Binding(
                    get: { model.enabled },
                    set: { model.setEnabled($0) }
                ))
                Text("The background service starts with your Mac and keeps the connection up even when this app is not running.")
                    .font(.caption)
                    .foregroundColor(.secondary)
            }
        }
        .formStyle(.grouped)
    }
}

// MARK: - Network

private struct NetworkTab: View {
    @EnvironmentObject var model: TetherModel
    @State private var saved = false

    private var dnsValid: Bool { isValidDNSList(model.service.dns) }

    var body: some View {
        Form {
            Section {
                Toggle("Send all internet traffic through the phone", isOn: $model.service.route_all)
                Text("Off brings the link up without changing your normal route, which is useful for testing.")
                    .font(.caption)
                    .foregroundColor(.secondary)
            }
            Section {
                TextField("DNS servers", text: $model.service.dns,
                          prompt: Text("Use the phone's, or e.g. 1.1.1.1, 8.8.8.8"))
                if !dnsValid {
                    Text("Enter IPv4 addresses separated by commas.")
                        .font(.caption)
                        .foregroundColor(.red)
                }
                Stepper(value: $model.service.mtu, in: 1200...1500, step: 20) {
                    Text("MTU: \(model.service.mtu)")
                }
                Text("Lower the MTU (for example 1400) if some sites hang while others load.")
                    .font(.caption)
                    .foregroundColor(.secondary)
            }
            Section {
                HStack {
                    Button("Apply") {
                        model.applyServiceSettings()
                        saved = true
                    }
                    .disabled(!dnsValid)
                    if saved {
                        Text("Saved. Reconnects if connected.")
                            .font(.caption)
                            .foregroundColor(.secondary)
                    }
                }
            }
        }
        .formStyle(.grouped)
        .onChange(of: model.service) { _ in saved = false }
    }
}

// MARK: - Service

private struct ServiceTab: View {
    @EnvironmentObject var model: TetherModel
    @State private var showLog = false
    @State private var confirmRemove = false

    private var statusText: String {
        switch model.state {
        case .notInstalled: return "Not installed"
        case .serviceDown: return "Installed, not responding"
        default: return "Running"
        }
    }

    var body: some View {
        Form {
            Section {
                LabeledContent("Status", value: statusText)
                LabeledContent("Installed version", value: model.installedVersion ?? "unknown")
                LabeledContent("This app's version", value: model.bundledVersion)
                if model.serviceNeedsUpdate {
                    Text("The installed service differs from this app's version. Use Repair / Update.")
                        .font(.caption)
                        .foregroundColor(.orange)
                }
            }
            Section {
                HStack {
                    Button(model.state == .notInstalled ? "Install Service..." : "Repair / Update Service...") {
                        model.installOrRepairService()
                    }
                    .disabled(model.busy)
                    Button("Remove Service...", role: .destructive) { confirmRemove = true }
                        .disabled(model.busy || model.state == .notInstalled)
                }
                if model.busy {
                    HStack(spacing: 8) {
                        ProgressView().controlSize(.small)
                        Text(model.busyText).font(.caption)
                    }
                }
                if let error = model.errorText {
                    Text(error)
                        .font(.caption)
                        .foregroundColor(.red)
                        .textSelection(.enabled)
                }
            }
            Section {
                HStack {
                    Button("Reconnect Now") { model.requestReconnect() }
                        .disabled(!(model.state == .connected || model.state == .reconnecting))
                    Button("Show Log...") { showLog = true }
                }
            }
        }
        .formStyle(.grouped)
        .sheet(isPresented: $showLog) { LogSheet() }
        .alert("Remove the background service?", isPresented: $confirmRemove) {
            Button("Remove", role: .destructive) { model.removeService() }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("USB tethering will stop working until you set it up again. Your network settings are restored.")
        }
    }
}

private struct LogSheet: View {
    @Environment(\.dismiss) private var dismiss
    @State private var text = ""

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text("Service log").font(.headline)
                Spacer()
                Button("Refresh") { text = ServiceInstaller.readLog() }
                Button("Copy") {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(text, forType: .string)
                }
                Button("Done") { dismiss() }
                    .keyboardShortcut(.defaultAction)
            }
            .padding()
            Divider()
            ScrollView {
                Text(text.isEmpty ? "(empty)" : text)
                    .font(.system(.caption, design: .monospaced))
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding()
            }
        }
        .frame(width: 560, height: 380)
        .onAppear { text = ServiceInstaller.readLog() }
    }
}

// MARK: - About

private struct AboutTab: View {
    private var versionText: String {
        let v = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "dev"
        return "Version \(v)"
    }

    var body: some View {
        VStack(spacing: 10) {
            Spacer()
            Image(nsImage: NSApp.applicationIconImage)
                .resizable()
                .frame(width: 72, height: 72)
            Text("USB Tether").font(.title.weight(.semibold))
            Text(versionText).foregroundColor(.secondary)
            Text("Use your Android phone's mobile data on this Mac over USB, with no drivers to install and no phone app.")
                .multilineTextAlignment(.center)
                .font(.callout)
                .foregroundColor(.secondary)
                .frame(maxWidth: 360)
            Spacer()
        }
        .frame(maxWidth: .infinity)
        .padding()
    }
}
