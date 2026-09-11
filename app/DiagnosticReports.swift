// SPDX-License-Identifier: GPL-3.0-only
import AppKit
import Darwin

/// Local-only reports. No network or mail operation happens during collection.
enum DiagnosticReport {
    static let recipient = "team@lighttable.app"
    static let unavailable = "No error trace was available. An unexpected exit alone does not establish its cause."

    static func read(_ url: URL, limit: Int = 64 * 1024) -> String {
        guard let file = try? FileHandle(forReadingFrom: url) else { return "" }
        defer { try? file.close() }
        guard let size = try? file.seekToEnd() else { return "" }
        try? file.seek(toOffset: size > UInt64(limit) ? size - UInt64(limit) : 0)
        return String(decoding: (try? file.read(upToCount: limit)) ?? Data(), as: UTF8.self)
    }

    /// Logs are untrusted text. Preserve stack locations, never source-code
    /// excerpts or photo paths. The preview remains available before sharing.
    static func redact(_ raw: String) -> String {
        var text = raw.removingPercentEncoding ?? raw
        let rules: [(String, String)] = [
            (#"(?im)(authorization|cookie|token|password|secret|api[_-]?key)\s*[:=].*$"#, "$1: [removed]"),
            (#"(?i)\bBearer\s+\S+"#, "Bearer [removed]"),
            (#"(?i)[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}"#, "[email]"),
            // Keep the code location in Python's stack format, including
            // native faulthandler output, without its installation path.
            (#"File \"[^\"\r\n]*[/\\]([^/\\\"\r\n]+\.py)\", line (\d+)"#, "File $1, line $2"),
            (#"(?i)(?:file://)?(?:[A-Z]:[/\\]|/(?:Users|Volumes|private|var|tmp|Applications|Library|home|mnt|media|opt|usr)/)[^\r\n]*"#, "[path removed]"),
            (#"(?i)[^\s\"'<>]*[/\\][^\s\"'<>]*"#, "[path]"),
            (#"(?i)[^\r\n\"']*\.(?:jpe?g|png|tiff?|heic|heif|avif|webp|dng|cr2|cr3|nef|arw|raf|orf|rw2|pef|srw|xmp)\b"#, "[photo]"),
            (#"\"[^\"\r\n]*\"|'[^'\r\n]*'"#, "[quoted value]")
        ]
        // Redact each line independently; an embedded newline cannot hide a
        // value from a rule intended to remove the remainder of its line.
        for (pattern, replacement) in rules {
            text = text.replacingOccurrences(of: pattern, with: replacement,
                                             options: .regularExpression)
        }
        return text
    }

    static func logExcerpt(_ raw: String) -> String {
        let lines = raw.components(separatedBy: .newlines).filter { line in
            let value = line.trimmingCharacters(in: .whitespaces)
            // Python tracebacks include a copy of the source line; omit it.
            if line.hasPrefix("    ") && !value.hasPrefix("File ") { return false }
            return !value.isEmpty
        }
        return redact(lines.suffix(160).joined(separator: "\n"))
    }

    static func hardware() -> String {
        var size = 0
        guard sysctlbyname("hw.model", nil, &size, nil, 0) == 0, size > 0 else { return "Unknown Mac" }
        var model = [CChar](repeating: 0, count: size)
        guard sysctlbyname("hw.model", &model, &size, nil, 0) == 0 else { return "Unknown Mac" }
        return String(cString: model)
    }

    static func buildDescription(bundle: Bundle = .main) -> String {
        let value = { (key: String) in bundle.object(forInfoDictionaryKey: key) as? String }
        let version = value("CFBundleShortVersionString") ?? "development"
        let build = value("CFBundleVersion") ?? "unknown"
        let revision = value("LightTableSourceRevision") ?? "unknown"
        let modified = (bundle.object(forInfoDictionaryKey: "LightTableSourceDirty") as? Bool)
            .map { $0 ? "yes" : "no" } ?? "unknown"
        return "LightTable \(version) (build \(build))\nSource revision: \(revision)\nLocal source changes: \(modified)\nmacOS: \(ProcessInfo.processInfo.operatingSystemVersionString)\nHardware: \(hardware()), \(ProcessInfo.processInfo.physicalMemory / (1024 * 1024 * 1024)) GB RAM"
    }

    static func emailURL() -> URL {
        var url = URLComponents()
        url.scheme = "mailto"
        url.path = recipient
        url.queryItems = [
            URLQueryItem(name: "subject", value: L("LightTable diagnostic report")),
            URLQueryItem(name: "body", value: L("What I was doing when the problem happened:\n\n\nDiagnostic report (paste the copied report below):\n\n"))
        ]
        return url.url!
    }
}

struct DiagnosticSession: Codable {
    var id = UUID().uuidString
    var startedAt = Date()
    var pid = ProcessInfo.processInfo.processIdentifier
    var executable = Bundle.main.executableURL?.path ?? CommandLine.arguments[0]
    var build = DiagnosticReport.buildDescription()
    var logPath: String
    var faultPath: String
    var catalogPath: String
}

struct DiagnosticIncident: Codable {
    var id: String
    var kind: String
    var startedAt: Date
    var detectedAt = Date()
    var pid: Int32
    var executable: String
    var report: String
    var presented = false
}

/// A separate advisory lock per native session distinguishes dead apps from
/// other live windows, even if macOS has reused a process ID. Session paths are
/// private local metadata; only the scrubbed report is copied or emailed.
final class DiagnosticStore {
    let root: URL
    private var session: DiagnosticSession?
    private var descriptor: Int32 = -1
    private let fm = FileManager.default
    private(set) var latest: DiagnosticIncident?
    var pending: DiagnosticIncident? { incidents().last(where: { !$0.presented }) }

    init(root: URL) { self.root = root }
    deinit { if descriptor >= 0 { close(descriptor) } }

    private func url(_ name: String) -> URL { root.appendingPathComponent(name) }
    private func write<T: Encodable>(_ value: T, to destination: URL) throws {
        let data = try JSONEncoder().encode(value)
        try data.write(to: destination, options: .atomic)
        try fm.setAttributes([.posixPermissions: 0o600], ofItemAtPath: destination.path)
    }
    private func decode<T: Decodable>(_ type: T.Type, at path: URL) -> T? {
        guard let size = try? path.resourceValues(forKeys: [.fileSizeKey]).fileSize,
              size <= 512 * 1024, let data = try? Data(contentsOf: path) else { return nil }
        return try? JSONDecoder().decode(type, from: data)
    }
    private func files(_ prefix: String) -> [URL] {
        ((try? fm.contentsOfDirectory(at: root, includingPropertiesForKeys: nil)) ?? [])
            .filter { $0.lastPathComponent.hasPrefix(prefix) && $0.pathExtension == "json" }
    }
    func incidents() -> [DiagnosticIncident] {
        files("incident-").compactMap { decode(DiagnosticIncident.self, at: $0) }
            .sorted { $0.detectedAt < $1.detectedAt }
    }
    func begin(log: URL, fault: URL, catalog: URL) {
        guard session == nil else { return }
        do {
            try fm.createDirectory(at: root, withIntermediateDirectories: true,
                                   attributes: [.posixPermissions: 0o700])
            for marker in files("session-").prefix(64) {
                let lock = marker.appendingPathExtension("lock")
                let fd = open(lock.path, O_CREAT | O_RDWR | O_CLOEXEC, 0o600)
                guard fd >= 0 else { continue }
                defer { close(fd) }
                guard flock(fd, LOCK_EX | LOCK_NB) == 0 else { continue }
                if let previous = decode(DiagnosticSession.self, at: marker) {
                    capture(id: previous.id, kind: "app", session: previous)
                }
                // Only consume the marker after its report was saved.
                if let previous = decode(DiagnosticSession.self, at: marker),
                   fm.fileExists(atPath: url("incident-\(previous.id).json").path) {
                    try? fm.removeItem(at: marker)
                    try? fm.removeItem(at: lock)
                    removeFaultLog(previous)
                }
            }
            let record = DiagnosticSession(logPath: log.path, faultPath: fault.path,
                                           catalogPath: catalog.path)
            let marker = url("session-\(record.id).json")
            descriptor = open(marker.appendingPathExtension("lock").path,
                              O_CREAT | O_RDWR | O_CLOEXEC, 0o600)
            guard descriptor >= 0, flock(descriptor, LOCK_EX | LOCK_NB) == 0 else { return }
            try write(record, to: marker)
            session = record
            latest = incidents().last
        } catch {
            // Bookkeeping failure must never prevent opening or saving photos.
            NSLog("LightTable could not save diagnostic session metadata.")
        }
    }
    func end() {
        guard let record = session else { return }
        let marker = url("session-\(record.id).json")
        try? fm.removeItem(at: marker)
        try? fm.removeItem(at: marker.appendingPathExtension("lock"))
        removeFaultLog(record)
        session = nil
        if descriptor >= 0 { close(descriptor); descriptor = -1 }
    }
    private func removeFaultLog(_ record: DiagnosticSession) {
        let file = URL(fileURLWithPath: record.faultPath).standardizedFileURL
        // Never delete an arbitrary path recovered from local metadata.
        guard file.deletingLastPathComponent() == root.standardizedFileURL,
              file.lastPathComponent == "engine-fault-\(record.pid).log" else { return }
        try? fm.removeItem(at: file)
    }
    func captureEngine(id: String, pid: Int32, executable: String,
                       startedAt: Date, status: Int32) {
        guard var record = session else { return }
        record.pid = pid
        record.executable = executable
        record.startedAt = startedAt
        capture(id: "engine-" + id, kind: "engine", session: record, status: status)
    }
    private func capture(id: String, kind: String, session: DiagnosticSession,
                         status: Int32? = nil) {
        let destination = url("incident-\(id).json")
        // Reopening logs / restarting repeatedly must not recreate an incident.
        guard !fm.fileExists(atPath: destination.path) else { return }
        let inflight = URL(fileURLWithPath: session.catalogPath).appendingPathComponent("inflight.json")
        let data = DiagnosticReport.read(inflight, limit: 8192).data(using: .utf8) ?? Data()
        let activity = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        let stage = activity?["stage"] as? String ?? "unknown"
        let operation = ["decode", "render", "export"].contains(stage) ? stage : "unknown"
        let stamp = ISO8601DateFormatter()
        var report = "LightTable diagnostic report\nIncident: \(id)\n\(session.build)\n\n"
        report += "Component: \(kind == "app" ? "native app" : "rendering engine")\n"
        report += "Session started: \(stamp.string(from: session.startedAt))\n"
        report += "Unexpected exit detected: \(stamp.string(from: Date()))\n"
        if let status { report += "Exit status: \(status)\n" }
        report += "Last recorded operation: \(operation)\n\n"
        let trace = DiagnosticReport.logExcerpt(DiagnosticReport.read(URL(fileURLWithPath: session.faultPath)))
        report += "Error trace:\n\(trace.isEmpty ? DiagnosticReport.unavailable : trace)\n\n"
        let log = DiagnosticReport.logExcerpt(DiagnosticReport.read(URL(fileURLWithPath: session.logPath)))
        report += "Recent server log (filtered):\n\(log.isEmpty ? "No log was available." : log)\n"
        let incident = DiagnosticIncident(id: id, kind: kind, startedAt: session.startedAt,
            pid: session.pid, executable: session.executable, report: report)
        do {
            try write(incident, to: destination)
            latest = incident
            for old in incidents().dropLast(10) {
                try? fm.removeItem(at: url("incident-\(old.id).json"))
            }
        } catch { NSLog("LightTable could not save a diagnostic report.") }
    }
    func markPresented(_ incident: DiagnosticIncident) {
        var record = incident
        record.presented = true
        try? write(record, to: url("incident-\(record.id).json"))
        latest = incidents().last
    }
    func manualReport(log: URL) -> String {
        "LightTable diagnostic report\n\(DiagnosticReport.buildDescription())\n\nNo recorded unexpected exit.\n\nRecent server log (filtered):\n"
            + DiagnosticReport.logExcerpt(DiagnosticReport.read(log))
    }
}

/// Match the process AND its launch interval. Never attach another app's
/// report, a same-name helper, or an old report for a reused PID. Collection is
/// on demand, off the main queue, and bounded to the newest candidates.
enum MacCrashReport {
    static func collect(for incident: DiagnosticIncident, directory: URL? = nil) -> String? {
        let root = directory ?? FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs/DiagnosticReports")
        let fm = FileManager.default
        let files = ((try? fm.contentsOfDirectory(at: root,
            includingPropertiesForKeys: [.contentModificationDateKey, .fileSizeKey])) ?? [])
            .filter { $0.pathExtension == "ips" }
            .compactMap { file -> (URL, Date)? in
                guard let values = try? file.resourceValues(forKeys: [.contentModificationDateKey, .fileSizeKey]),
                      let date = values.contentModificationDate,
                      (values.fileSize ?? Int.max) <= 2 * 1024 * 1024,
                      date >= incident.startedAt, date <= incident.detectedAt.addingTimeInterval(120)
                else { return nil }
                return (file, date)
            }.sorted { $0.1 > $1.1 }.prefix(40)
        for (file, _) in files {
            guard let data = try? Data(contentsOf: file),
                  let text = String(data: data, encoding: .utf8) else { continue }
            // Modern .ips contains a one-line header followed by the payload.
            let payload = text.split(separator: "\n", maxSplits: 1).last.map(String.init) ?? text
            guard let bytes = payload.data(using: .utf8),
                  let report = (try? JSONSerialization.jsonObject(with: bytes)) as? [String: Any],
                  (report["pid"] as? NSNumber)?.int32Value == incident.pid,
                  report["procPath"] as? String == incident.executable else { continue }
            // Deliberately exclude account IDs, paths, queues, registers, VM
            // contents, and application-specific strings. Retain symbol and
            // binary UUID information useful for symbolication.
            var clean: [String: Any] = [:]
            for key in ["exception", "termination", "faultingThread"] { clean[key] = report[key] }
            clean["threads"] = (report["threads"] as? [[String: Any]] ?? []).prefix(64).map { thread in
                ["triggered": thread["triggered"] ?? false,
                 "frames": (thread["frames"] as? [[String: Any]] ?? []).prefix(100).map { frame in
                    frame.filter { ["imageIndex", "imageOffset", "symbol", "symbolLocation"].contains($0.key) }
                 }] as [String: Any]
            }
            clean["usedImages"] = (report["usedImages"] as? [[String: Any]] ?? []).map { image in
                image.filter { ["name", "uuid", "arch", "base", "size"].contains($0.key) }
            }
            // Preserve JSON quotes while still filtering string values.
            func scrub(_ value: Any) -> Any {
                if let string = value as? String { return DiagnosticReport.redact(string) }
                if let array = value as? [Any] { return array.map(scrub) }
                if let object = value as? [String: Any] { return object.mapValues(scrub) }
                return value
            }
            guard let scrubbed = try? JSONSerialization.data(withJSONObject: scrub(clean), options: [.prettyPrinted, .sortedKeys]),
                  let filtered = String(data: scrubbed, encoding: .utf8) else { continue }
            return String(filtered.prefix(96 * 1024))
        }
        return nil
    }
}

/// A nonmodal panel also works when the web interface or server cannot start.
final class DiagnosticReportWindow: NSWindowController {
    private let reportView = NSTextView()
    private let status = NSTextField(wrappingLabelWithString: "")
    private let pasteboard: NSPasteboard
    private let openEmail: (URL) -> Bool

    init(report: String, incident: DiagnosticIncident?,
         pasteboard: NSPasteboard = .general,
         openEmail: @escaping (URL) -> Bool = { NSWorkspace.shared.open($0) }) {
        self.pasteboard = pasteboard
        self.openEmail = openEmail
        let panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 680, height: 560),
                            styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
        panel.title = L("LightTable Diagnostic Report")
        panel.minSize = NSSize(width: 600, height: 440)
        panel.isReleasedWhenClosed = false
        super.init(window: panel)
        let title = NSTextField(labelWithString: incident == nil ? L("Report a problem") :
            (incident?.kind == "app" ? L("LightTable closed unexpectedly last time.") : L("LightTable’s rendering engine stopped unexpectedly.")))
        title.font = .boldSystemFont(ofSize: 16)
        title.lineBreakMode = .byWordWrapping
        title.maximumNumberOfLines = 0
        let explanation = NSTextField(wrappingLabelWithString:
            L("You can help fix the problem by emailing this report to {recipient}. Nothing is sent automatically. Review the report before sharing; paths and photo filenames are filtered out.", ["recipient": DiagnosticReport.recipient]))
        let scroll = NSScrollView()
        scroll.hasVerticalScroller = true
        scroll.borderType = .bezelBorder
        reportView.isEditable = false
        reportView.isSelectable = true
        reportView.isRichText = false
        reportView.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
        reportView.textContainerInset = NSSize(width: 10, height: 10)
        reportView.autoresizingMask = [.width]
        reportView.textContainer?.widthTracksTextView = true
        reportView.string = report
        scroll.documentView = reportView
        let copy = NSButton(title: L("Copy report"), target: self, action: #selector(copyReport))
        let email = NSButton(title: L("Email maintainer"), target: self, action: #selector(emailReport))
        let dismiss = NSButton(title: L("Dismiss"), target: self, action: #selector(dismissReport))
        dismiss.keyEquivalent = "\u{1b}"
        let buttons = NSStackView(views: [copy, email, dismiss])
        buttons.orientation = .horizontal
        buttons.spacing = 10
        status.stringValue = L("Email maintainer copies the report and opens a draft. Paste the report into the email before sending.")
        status.font = .systemFont(ofSize: 11)
        status.textColor = .secondaryLabelColor
        let content = NSStackView(views: [title, explanation, scroll, status, buttons])
        content.orientation = .vertical
        content.alignment = .leading
        content.spacing = 14
        content.translatesAutoresizingMaskIntoConstraints = false
        panel.contentView!.addSubview(content)
        NSLayoutConstraint.activate([
            content.leadingAnchor.constraint(equalTo: panel.contentView!.leadingAnchor, constant: 22),
            content.trailingAnchor.constraint(equalTo: panel.contentView!.trailingAnchor, constant: -22),
            content.topAnchor.constraint(equalTo: panel.contentView!.topAnchor, constant: 22),
            content.bottomAnchor.constraint(equalTo: panel.contentView!.bottomAnchor, constant: -22),
            scroll.widthAnchor.constraint(equalTo: content.widthAnchor),
            title.widthAnchor.constraint(equalTo: content.widthAnchor),
            explanation.widthAnchor.constraint(equalTo: content.widthAnchor),
            status.widthAnchor.constraint(equalTo: content.widthAnchor),
            scroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 160)
        ])
        panel.center()
        if let incident {
            copy.isEnabled = false
            email.isEnabled = false
            status.stringValue = L("Checking for a matching macOS crash report…")
            DispatchQueue.global(qos: .utility).async { [weak self] in
                let native = MacCrashReport.collect(for: incident)
                DispatchQueue.main.async {
                    self?.reportView.string += "\nmacOS crash report:\n" + (native ?? "No matching macOS crash report was available.") + "\n"
                    copy.isEnabled = true
                    email.isEnabled = true
                    self?.status.stringValue = L("Email maintainer copies the report and opens a draft. Paste the report into the email before sending.")
                }
            }
        }
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    private func copyToClipboard() -> Bool {
        pasteboard.clearContents()
        if pasteboard.setString(reportView.string, forType: .string) {
            status.stringValue = L("Report copied. Paste it into an email to {recipient}.", ["recipient": DiagnosticReport.recipient])
            return true
        }
        status.stringValue = L("The report could not be copied. Select the text and copy it manually.")
        return false
    }
    @objc private func copyReport() { _ = copyToClipboard() }
    @objc private func emailReport() {
        guard copyToClipboard() else { return }
        if !openEmail(DiagnosticReport.emailURL()) {
            status.stringValue = L("No email app opened. Paste the copied report into an email to {recipient}.", ["recipient": DiagnosticReport.recipient])
        }
    }
    @objc private func dismissReport() { close() }
}
