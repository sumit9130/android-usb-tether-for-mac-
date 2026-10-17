import AppKit
import SwiftUI

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        // Menu bar only: no Dock icon (also set with LSUIElement in Info.plist).
        NSApp.setActivationPolicy(.accessory)
    }
}

/// The menu bar icon, optionally with the live speed next to it.
struct MenuBarLabel: View {
    @ObservedObject var model: TetherModel
    @ObservedObject var prefs: AppPrefs

    var body: some View {
        HStack(spacing: 4) {
            Image(systemName: model.menuSymbol)
            if prefs.showSpeedInMenuBar && model.state == .connected {
                Text("↓\(formatSpeed(model.downSpeed))")
                    .monospacedDigit()
            }
        }
    }
}

@main
struct USBTetherApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var model = TetherModel()

    var body: some Scene {
        MenuBarExtra {
            PopoverView()
                .environmentObject(model)
        } label: {
            MenuBarLabel(model: model, prefs: model.prefs)
        }
        .menuBarExtraStyle(.window)
    }
}

/// Shows the settings window on demand. (A SwiftUI `Window` scene would open by itself at
/// launch, which a menu bar app must not do.)
final class SettingsWindowController {
    static let shared = SettingsWindowController()
    private var window: NSWindow?

    func show(model: TetherModel) {
        if window == nil {
            let content = SettingsView()
                .environmentObject(model)
                .environmentObject(model.prefs)
            let w = NSWindow(contentViewController: NSHostingController(rootView: content))
            w.title = "USB Tether Settings"
            w.styleMask = [.titled, .closable]
            w.isReleasedWhenClosed = false
            w.center()
            window = w
        }
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
    }
}
