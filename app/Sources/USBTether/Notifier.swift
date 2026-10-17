import Foundation
import UserNotifications

/// Thin wrapper around user notifications. Does nothing when not running as a real app bundle
/// (for example under `swift run`), where UNUserNotificationCenter would crash.
final class Notifier {
    static let shared = Notifier()

    private var available: Bool {
        Bundle.main.bundleIdentifier != nil && Bundle.main.bundlePath.hasSuffix(".app")
    }

    func requestAuthorization() {
        guard available else { return }
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { _, _ in }
    }

    func post(_ title: String, _ body: String) {
        guard available else { return }
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        let request = UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil)
        UNUserNotificationCenter.current().add(request)
    }
}
