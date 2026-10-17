import Foundation

/// Files shared with the background service (see usbtether.py).
enum Paths {
    static let controlDir = "/Users/Shared/usbtether"
    static let status = controlDir + "/status.json"
    static let enabled = controlDir + "/enabled"
    static let reconnect = controlDir + "/reconnect"
    static let settings = controlDir + "/settings.json"
    static let daemonPlist = "/Library/LaunchDaemons/com.usbtether.daemon.plist"
    static let log = "/var/log/usbtether.log"
}

// MARK: - formatting

private let byteFormatter: ByteCountFormatter = {
    let f = ByteCountFormatter()
    f.countStyle = .binary
    f.allowsNonnumericFormatting = false   // "0 KB" instead of "Zero KB"
    return f
}()

func formatBytes(_ n: Int64) -> String {
    byteFormatter.string(fromByteCount: max(0, n))
}

func formatSpeed(_ bytesPerSecond: Double) -> String {
    byteFormatter.string(fromByteCount: Int64(max(0, bytesPerSecond))) + "/s"
}

func formatDuration(_ seconds: TimeInterval) -> String {
    let s = Int(max(0, seconds))
    if s < 60 { return "\(s)s" }
    if s < 3600 { return "\(s / 60)m \(s % 60)s" }
    return "\(s / 3600)h \((s % 3600) / 60)m"
}

// MARK: - validation

/// True for an empty string or a comma-separated list of IPv4 addresses.
func isValidDNSList(_ text: String) -> Bool {
    let trimmed = text.trimmingCharacters(in: .whitespaces)
    if trimmed.isEmpty { return true }
    for part in trimmed.split(separator: ",") {
        var addr = in_addr()
        let candidate = part.trimmingCharacters(in: .whitespaces)
        if inet_pton(AF_INET, candidate, &addr) != 1 { return false }
    }
    return true
}
