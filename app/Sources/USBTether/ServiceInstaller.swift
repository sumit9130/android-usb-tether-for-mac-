import Foundation

enum InstallResult {
    case success
    case failure(String)
}

/// Installs, repairs and removes the background service by running the bundled scripts
/// through the standard macOS administrator password prompt.
enum ServiceInstaller {
    static var serviceDir: URL? {
        Bundle.main.resourceURL?.appendingPathComponent("service")
    }

    /// The service version this app ships with.
    static func bundledVersion() -> String {
        guard let url = serviceDir?.appendingPathComponent("VERSION"),
              let text = try? String(contentsOf: url, encoding: .utf8) else { return "0" }
        return text.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// Runs setup.sh or remove.sh. `completion` is called on the main thread.
    static func run(script: String, withUser: Bool, completion: @escaping (InstallResult) -> Void) {
        guard let dir = serviceDir?.path else {
            completion(.failure("The app is missing its service files. Rebuild it with build_app.sh."))
            return
        }
        var command = "/bin/bash \(shellQuote(dir + "/" + script)) \(shellQuote(dir))"
        if withUser { command += " \(shellQuote(NSUserName()))" }

        DispatchQueue.global(qos: .userInitiated).async {
            let result = runAsAdmin(command)
            DispatchQueue.main.async {
                completion(result.ok ? .success : .failure(friendly(result.output)))
            }
        }
    }

    static func readLog(maxBytes: UInt64 = 96 * 1024) -> String {
        guard let handle = FileHandle(forReadingAtPath: Paths.log) else {
            return "No log yet. The service has not written anything."
        }
        defer { try? handle.close() }
        let size = (try? handle.seekToEnd()) ?? 0
        try? handle.seek(toOffset: size > maxBytes ? size - maxBytes : 0)
        let data = (try? handle.readToEnd()) ?? Data()
        return String(decoding: data, as: UTF8.self)
    }

    // MARK: - helpers

    private static func shellQuote(_ s: String) -> String {
        "'" + s.replacingOccurrences(of: "'", with: "'\\''") + "'"
    }

    private static func runAsAdmin(_ command: String) -> (ok: Bool, output: String) {
        let escaped = command
            .replacingOccurrences(of: "\\", with: "\\\\")
            .replacingOccurrences(of: "\"", with: "\\\"")
        let script = "do shell script \"\(escaped)\" with administrator privileges "
            + "with prompt \"USB Tether needs your permission to manage its background service.\""

        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        process.arguments = ["-e", script]
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        do {
            try process.run()
        } catch {
            return (false, "Could not start the installer: \(error.localizedDescription)")
        }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        return (process.terminationStatus == 0, String(decoding: data, as: UTF8.self))
    }

    private static func friendly(_ output: String) -> String {
        let text = output.trimmingCharacters(in: .whitespacesAndNewlines)
        if text.contains("User canceled") || text.contains("(-128)") {
            return "Cancelled. Nothing was changed."
        }
        if text.isEmpty { return "The installer failed without a message." }
        return text.count > 700 ? "..." + String(text.suffix(700)) : text
    }
}
