import Foundation
import ServiceManagement

/// Settings for the app itself (stored in UserDefaults).
final class AppPrefs: ObservableObject {
    @Published var notificationsEnabled: Bool {
        didSet {
            UserDefaults.standard.set(notificationsEnabled, forKey: "notifications")
            if notificationsEnabled { Notifier.shared.requestAuthorization() }
        }
    }

    @Published var showSpeedInMenuBar: Bool {
        didSet { UserDefaults.standard.set(showSpeedInMenuBar, forKey: "showSpeedInMenuBar") }
    }

    @Published private(set) var launchAtLogin: Bool

    init() {
        let d = UserDefaults.standard
        d.register(defaults: ["notifications": true, "showSpeedInMenuBar": false])
        notificationsEnabled = d.bool(forKey: "notifications")
        showSpeedInMenuBar = d.bool(forKey: "showSpeedInMenuBar")
        launchAtLogin = SMAppService.mainApp.status == .enabled
    }

    /// Returns an error message if the change failed.
    @discardableResult
    func setLaunchAtLogin(_ on: Bool) -> String? {
        var message: String?
        do {
            if on {
                try SMAppService.mainApp.register()
            } else {
                try SMAppService.mainApp.unregister()
            }
        } catch {
            message = "Could not change the login item: \(error.localizedDescription)"
        }
        launchAtLogin = SMAppService.mainApp.status == .enabled
        return message
    }
}

/// Settings for the background service (settings.json, read by usbtether.py each time it connects).
struct ServiceSettings: Codable, Equatable {
    var route_all: Bool = true
    var dns: String = ""
    var mtu: Int = 1500

    init() {}

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        route_all = try c.decodeIfPresent(Bool.self, forKey: .route_all) ?? true
        dns = try c.decodeIfPresent(String.self, forKey: .dns) ?? ""
        mtu = try c.decodeIfPresent(Int.self, forKey: .mtu) ?? 1500
    }

    static func load() -> ServiceSettings {
        guard let data = FileManager.default.contents(atPath: Paths.settings),
              let s = try? JSONDecoder().decode(ServiceSettings.self, from: data) else {
            return ServiceSettings()
        }
        return s
    }

    func save() throws {
        var s = self
        s.dns = s.dns.trimmingCharacters(in: .whitespaces)
        s.mtu = min(1500, max(576, s.mtu))
        let data = try JSONEncoder().encode(s)
        try data.write(to: URL(fileURLWithPath: Paths.settings), options: .atomic)
    }
}
