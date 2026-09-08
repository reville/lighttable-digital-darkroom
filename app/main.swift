// LightTable.app — native shell around the local Python render server.
//
// The bundle owns the native window, updater, local Python runtime, and
// render-server lifecycle.

import AppKit
import Darwin
import CryptoKit
import Photos
import PhotosUI
import UniformTypeIdentifiers
import UserNotifications
import WebKit
#if canImport(Sparkle)
import Sparkle
#endif

private enum WindowChrome {
    static let topBarHeight: CGFloat = 48
    // A plain titled window uses a tight inset intended for a short title bar.
    // Take the system metrics from a unified toolbar for our full-height header.
    static let nativeTrafficLightFrames: [NSRect] = {
        let reference = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 800, height: 600),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered, defer: false)
        reference.toolbar = NSToolbar(identifier: "LightTableChromeMetrics")
        reference.toolbarStyle = .unified
        reference.contentView?.superview?.layoutSubtreeIfNeeded()
        return [NSWindow.ButtonType.closeButton, .miniaturizeButton, .zoomButton]
            .compactMap { reference.standardWindowButton($0)?.frame }
    }()
    static var trafficLightClearance: CGFloat {
        guard let first = nativeTrafficLightFrames.first,
              let last = nativeTrafficLightFrames.last else { return 98 }
        return ceil(last.maxX + first.minX)
    }
    static let fallbackBottomDragStripHeight: CGFloat = 8
    static let controlSafetyInset: CGFloat = 2
}

final class LightTableWebView: WKWebView {
    private var topBarRect: CGRect?
    private var topBarControlRects: [CGRect] = []
    private var windowChromeDragBlocked = false

    func updateWindowChromeLayout(_ body: [String: Any]) {
        guard let topBar = Self.rect(body["topBar"]),
              let controls = body["controls"] as? [[String: Any]] else { return }
        let controlRects = controls.compactMap(Self.rect)
        topBarRect = topBar
        topBarControlRects = controlRects
        windowChromeDragBlocked = body["blocked"] as? Bool ?? false
    }

    func resetWindowChromeLayout() {
        topBarRect = nil
        topBarControlRects = []
        windowChromeDragBlocked = false
    }

    // WKWebView's internal content views receive mouseDown before the outer
    // view does. Claim only background chrome during AppKit hit testing so
    // WebKit keeps every interactive control and the photo workspace.
    override func hitTest(_ point: NSPoint) -> NSView? {
        guard let hit = super.hitTest(point) else { return nil }
        return isWindowChromeBackground(convert(point, from: superview)) ? self : hit
    }

    private func isWindowChromeBackground(_ point: NSPoint) -> Bool {
        guard bounds.contains(point), !windowChromeDragBlocked else { return false }
        // DOM rectangles are top-down; WKWebView is flipped on macOS.
        let distanceFromTop = isFlipped ? point.y - bounds.minY : bounds.maxY - point.y
        let pagePoint = CGPoint(x: point.x - bounds.minX, y: distanceFromTop)
        let hasLiveLayout = topBarRect != nil
        let isInTopBar = topBarRect?.contains(pagePoint)
            ?? (0...WindowChrome.topBarHeight).contains(distanceFromTop)
        let isOverControl = topBarControlRects.contains {
            $0.insetBy(dx: -WindowChrome.controlSafetyInset,
                       dy: -WindowChrome.controlSafetyInset).contains(pagePoint)
        }
        let isFallbackBackground = pagePoint.x < WindowChrome.trafficLightClearance
            || distanceFromTop >= WindowChrome.topBarHeight
                - WindowChrome.fallbackBottomDragStripHeight
        let isChromeBackground = hasLiveLayout
            ? !isOverControl
            : isFallbackBackground
        return isInTopBar && isChromeBackground
    }

    override func mouseDown(with event: NSEvent) {
        if isWindowChromeBackground(convert(event.locationInWindow, from: nil)), let window {
            if event.clickCount == 2 {
                performTitleBarDoubleClick(on: window)
                return
            }
            window.performDrag(with: event)
            return
        }
        super.mouseDown(with: event)
    }

    private static func rect(_ value: Any?) -> CGRect? {
        guard let value = value as? [String: Any],
              let x = value["x"] as? NSNumber,
              let y = value["y"] as? NSNumber,
              let width = value["width"] as? NSNumber,
              let height = value["height"] as? NSNumber,
              width.doubleValue > 0, height.doubleValue > 0 else { return nil }
        return CGRect(x: x.doubleValue, y: y.doubleValue,
                      width: width.doubleValue, height: height.doubleValue)
    }

    private func performTitleBarDoubleClick(on window: NSWindow) {
        switch UserDefaults.standard.string(
            forKey: "AppleActionOnDoubleClick")?.lowercased() {
        case "minimize":
            window.miniaturize(nil)
        case "none":
            break
        default:
            window.performZoom(nil)
        }
    }
}

// MARK: - Locations

let projectDir: URL = {
    if let resources = Bundle.main.resourceURL {
        let bundled = resources.appendingPathComponent("LightTable", isDirectory: true)
        if FileManager.default.fileExists(
            atPath: bundled.appendingPathComponent("server.py").path) {
            return bundled
        }
    }
    let adjacentProject = Bundle.main.bundleURL
        .deletingLastPathComponent()
        .deletingLastPathComponent()
    if FileManager.default.fileExists(
        atPath: adjacentProject.appendingPathComponent("server.py").path) {
        return adjacentProject
    }
    let fromPlist = Bundle.main.object(
        forInfoDictionaryKey: "LightTableProjectDir") as? String
    if let fromPlist, !fromPlist.isEmpty {
        return URL(fileURLWithPath:
            (fromPlist as NSString).expandingTildeInPath)
    }
    // Older developer shells did not record their checkout in Info.plist.
    // Accept both project names while release bundles remain self-contained.
    let legacyPaths = ["~/CODING/Film Lab/app", "~/CODING/LightTable/app"]
    for path in legacyPaths {
        let candidate = URL(fileURLWithPath:
            (path as NSString).expandingTildeInPath)
        if FileManager.default.fileExists(
            atPath: candidate.appendingPathComponent("server.py").path) {
            return candidate
        }
    }
    return URL(fileURLWithPath:
        (legacyPaths[0] as NSString).expandingTildeInPath)
}()

let defaultPhotoFolder: String = {
    FileManager.default.urls(for: .picturesDirectory, in: .userDomainMask).first?.path
        ?? (NSHomeDirectory() as NSString).appendingPathComponent("Pictures")
}()

// BEGIN NATIVE LOCALIZATION CORE — Foundation only; exercised by native tests.
struct NativeLanguage: Decodable {
    let code: String
    let name: String
    let nativeName: String
    let dir: String
}

final class NativeLocaleStore {
    private struct Manifest: Decodable {
        let version: Int
        let sourceLocale: String
        let locales: [NativeLanguage]
    }
    private struct Catalog: Decodable {
        let version: Int
        let locale: String
        let messages: [String: String]
    }
    let directory: URL
    let preferencesURL: URL
    let languages: [NativeLanguage]
    private(set) var locale = "en"
    private var messages: [String: String] = [:]
    private let messageLock = NSLock()

    init(directory: URL, preferencesURL: URL) {
        self.directory = directory
        self.preferencesURL = preferencesURL
        let manifest = (try? Data(contentsOf: directory.appendingPathComponent("manifest.json")))
            .flatMap { try? JSONDecoder().decode(Manifest.self, from: $0) }
        var seen = Set<String>()
        let valid = manifest?.version == 1 && manifest?.sourceLocale == "en"
            ? (manifest?.locales ?? []).filter {
                Self.safeCode($0.code) && !$0.nativeName.isEmpty &&
                ["ltr", "rtl"].contains($0.dir) && seen.insert($0.code).inserted
            } : []
        languages = valid.contains(where: { $0.code == "en" }) ? valid :
            [NativeLanguage(code: "en", name: "English", nativeName: "English", dir: "ltr")]
        try? reload()
    }

    static func safeCode(_ value: String) -> Bool {
        value.range(of: "^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$", options: .regularExpression) != nil
    }

    func supported(_ value: String) -> String? {
        languages.first { $0.code.caseInsensitiveCompare(value) == .orderedSame }?.code
    }

    func suggested(for language: String) -> String {
        let tag = language.replacingOccurrences(of: "_", with: "-")
            .split(separator: ".").first.map(String.init) ?? language
        if let exact = supported(tag) { return exact }
        let pieces = tag.lowercased().split(separator: "-").map(String.init)
        if pieces.first == "zh" {
            let traditional = pieces.contains("hant") ||
                (!pieces.contains("hans") && pieces.contains(where: { ["tw", "hk", "mo"].contains($0) }))
            if let chinese = supported(traditional ? "zh-Hant" : "zh-Hans") { return chinese }
        }
        return supported(pieces.first ?? "en") ?? "en"
    }

    static func isEnglish(_ language: String) -> Bool {
        language.lowercased().replacingOccurrences(of: "_", with: "-")
            .split(separator: "-").first == "en"
    }

    func preferences() throws -> [String: Any] {
        guard FileManager.default.fileExists(atPath: preferencesURL.path) else { return [:] }
        let value = try JSONSerialization.jsonObject(with: Data(contentsOf: preferencesURL))
        guard let object = value as? [String: Any] else {
            throw NSError(domain: "LightTable.Localization", code: 1,
                          userInfo: [NSLocalizedDescriptionKey: "The preferences file is not a JSON object."])
        }
        return object
    }

    func reload() throws {
        let prefs = try preferences()
        setLocale(supported(prefs["locale"] as? String ?? "") ?? "en")
    }

    func setLocale(_ code: String) {
        messageLock.lock()
        defer { messageLock.unlock() }
        locale = supported(code) ?? "en"
        messages = [:]
        guard locale != "en", Self.safeCode(locale),
              let data = try? Data(contentsOf: directory.appendingPathComponent(locale + ".json")),
              let catalog = try? JSONDecoder().decode(Catalog.self, from: data),
              catalog.version == 1, catalog.locale == locale else { return }
        messages = catalog.messages.filter {
            !$0.value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty &&
                Self.placeholders($0.key) == Self.placeholders($0.value)
        }
    }

    func saveChoice(_ code: String) throws {
        guard let selected = supported(code) else {
            throw NSError(domain: "LightTable.Localization", code: 2,
                          userInfo: [NSLocalizedDescriptionKey: "This language is not available."])
        }
        // Read the full object immediately before writing. A malformed or unreadable
        // file must never be replaced with a locale-only preferences document.
        var prefs = try preferences()
        prefs["locale"] = selected
        prefs["localeChosen"] = true
        let encoded = try JSONSerialization.data(withJSONObject: prefs, options: [.prettyPrinted, .sortedKeys])
        try FileManager.default.createDirectory(at: preferencesURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        try encoded.write(to: preferencesURL, options: .atomic)
        setLocale(selected)
    }

    static func placeholders(_ value: String) -> Set<String> {
        guard let regex = try? NSRegularExpression(pattern: "\\{[A-Za-z][A-Za-z0-9_]*\\}") else { return [] }
        return Set(regex.matches(in: value, range: NSRange(value.startIndex..., in: value))
            .compactMap { Range($0.range, in: value).map { String(value[$0]) } })
    }

    func text(_ source: String, _ arguments: [String: String] = [:]) -> String {
        messageLock.lock()
        let translated = messages[source] ?? source
        messageLock.unlock()
        // Replace only placeholders from the template, never text inside an argument.
        guard let regex = try? NSRegularExpression(pattern: "\\{([A-Za-z][A-Za-z0-9_]*)\\}") else { return translated }
        var result = translated
        for match in regex.matches(in: translated, range: NSRange(translated.startIndex..., in: translated)).reversed() {
            guard let keyRange = Range(match.range(at: 1), in: translated),
                  let value = arguments[String(translated[keyRange])],
                  let range = Range(match.range, in: result) else { continue }
            result.replaceSubrange(range, with: value)
        }
        return result
    }
}
// END NATIVE LOCALIZATION CORE

private func lightTableSupportDirectory() -> URL {
    FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first!
        .appendingPathComponent("LightTable", isDirectory: true)
}
private func lightTablePreferencesURL() -> URL {
    if let path = ProcessInfo.processInfo.environment["LIGHTTABLE_PREFS_FILE"], !path.isEmpty {
        return URL(fileURLWithPath: path)
    }
    return lightTableSupportDirectory().appendingPathComponent("prefs.json")
}
private let nativeLocalization = NativeLocaleStore(
    directory: projectDir.appendingPathComponent("web/locales", isDirectory: true),
    preferencesURL: lightTablePreferencesURL())
private func L(_ source: String, _ arguments: [String: String] = [:]) -> String {
    nativeLocalization.text(source, arguments)
}

struct FolderSource: Codable, Equatable {
    var path: String
    var favorite: Bool
}

// MARK: - Server

final class ServerController {
    private var process: Process?
    private(set) var port: Int = 0
    let logURL: URL = {
        if let path = ProcessInfo.processInfo.environment[
            "LIGHTTABLE_SERVER_LOG"], !path.isEmpty {
            return URL(fileURLWithPath: path)
        }
        return FileManager.default.temporaryDirectory
            .appendingPathComponent("lighttable-server.log")
    }()

    /// The server reports its startup phase here, so the shell can show
    /// what it is doing and stop waiting the moment it reports a failure.
    private(set) var startupReportURL: URL?
    /// Safe mode starts the server with every background service off.
    var safeMode = false
    /// Passed to the next server so its crash ledger records how the last
    /// one ended.
    var previousExitStatus: Int32?
    /// Called on the main queue when the server exits without being asked.
    var onUnexpectedExit: ((Int32) -> Void)?
    private var stopping = false
    private var startedAt = Date()

    /// The server exits with this status when it wants a clean relaunch,
    /// for instance after replacing the catalog file. It is not a crash.
    static let restartExitStatus: Int32 = 75

    struct StartupReport {
        let phase: String
        let detail: String?
        let code: String?
        let hint: String?
        let port: Int?
        let holder: [String: Any]?
    }

    /// Where the catalog and its Recovery folder live for this server.
    var catalogDirectory: URL {
        if let path = ProcessInfo.processInfo.environment[
            "LIGHTTABLE_CATALOG_FILE"], !path.isEmpty {
            return URL(fileURLWithPath: path).deletingLastPathComponent()
        }
        return supportDirectory.appendingPathComponent("Catalog", isDirectory: true)
    }

    var uptime: TimeInterval { Date().timeIntervalSince(startedAt) }

    var python: URL {
        if let resources = Bundle.main.resourceURL {
            let bundled = resources.appendingPathComponent("Python/bin/python3")
            if FileManager.default.isExecutableFile(atPath: bundled.path) {
                return bundled
            }
        }
        return projectDir.appendingPathComponent(".venv/bin/python")
    }
    var script: URL { projectDir.appendingPathComponent("server.py") }

    private var supportDirectory: URL {
        lightTableSupportDirectory()
    }

    private var cacheDirectory: URL {
        let root = FileManager.default.urls(
            for: .cachesDirectory, in: .userDomainMask).first!
        return root.appendingPathComponent("LightTable", isDirectory: true)
    }

    var isInstalled: Bool {
        FileManager.default.isExecutableFile(atPath: python.path)
            && FileManager.default.fileExists(atPath: script.path)
    }

    static let preferredPort = 8321

    /// Bind a loopback port; 0 asks the kernel for any free one.
    /// Returns the bound port, or nil if unavailable.
    private func probe(_ wanted: Int) -> Int? {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { return nil }
        defer { close(fd) }
        var addr = sockaddr_in()
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_addr.s_addr = inet_addr("127.0.0.1")
        addr.sin_port = UInt16(wanted).bigEndian
        let ok = withUnsafePointer(to: &addr) { p in
            p.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        guard ok == 0 else { return nil }
        var out = sockaddr_in()
        var len = socklen_t(MemoryLayout<sockaddr_in>.size)
        let got = withUnsafeMutablePointer(to: &out) { p in
            p.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                getsockname(fd, $0, &len)
            }
        }
        guard got == 0 else { return nil }
        return Int(UInt16(bigEndian: out.sin_port))
    }

    /// Keep the port stable so a browser tab or bookmark stays valid;
    /// only fall back to a random one if 8321 is genuinely taken.
    private func choosePort() -> Int {
        if let p = probe(Self.preferredPort) { return p }
        return probe(0) ?? Self.preferredPort
    }

    /// Keep the last few launches' logs. Truncating on every start erased
    /// the one log that explained why the previous launch failed.
    private func rotateLog() {
        let fm = FileManager.default
        guard let attributes = try? fm.attributesOfItem(atPath: logURL.path),
              let size = attributes[.size] as? Int, size > 0 else { return }
        func generation(_ index: Int) -> URL {
            logURL.appendingPathExtension("\(index)")
        }
        try? fm.removeItem(at: generation(3))
        for index in stride(from: 2, through: 1, by: -1) {
            let older = generation(index)
            if fm.fileExists(atPath: older.path) {
                try? fm.moveItem(at: older, to: generation(index + 1))
            }
        }
        try? fm.moveItem(at: logURL, to: generation(1))
    }

    func start(folder: String) throws {
        stop()
        stopping = false
        port = choosePort()
        try FileManager.default.createDirectory(
            at: supportDirectory, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(
            at: cacheDirectory, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(
            at: logURL.deletingLastPathComponent(),
            withIntermediateDirectories: true)
        rotateLog()
        FileManager.default.createFile(atPath: logURL.path, contents: nil)
        let log = try FileHandle(forWritingTo: logURL)
        let stamp = ISO8601DateFormatter().string(from: Date())
        let header = "=== LightTable launch \(stamp) port \(port)"
            + (safeMode ? " safe mode" : "")
            + (previousExitStatus.map { " after exit \($0)" } ?? "")
            + " ===\n"
        log.write(Data(header.utf8))

        let instances = supportDirectory.appendingPathComponent(
            "instances", isDirectory: true)
        try FileManager.default.createDirectory(
            at: instances, withIntermediateDirectories: true)
        let launchID = UUID().uuidString
        startupReportURL = instances.appendingPathComponent(
            "startup-\(launchID).json")

        let p = Process()
        p.executableURL = python
        p.arguments = [script.path]
        p.currentDirectoryURL = projectDir
        var env = ProcessInfo.processInfo.environment
        env["LIGHTTABLE_DIR"] = folder
        env["LIGHTTABLE_PORT"] = String(port)
        env["LIGHTTABLE_WATCH_PARENT"] = "1"
        env["LIGHTTABLE_PARENT_PID"] = String(ProcessInfo.processInfo.processIdentifier)
        env["LIGHTTABLE_STARTUP_FILE"] = startupReportURL!.path
        if safeMode { env["LIGHTTABLE_SAFE_MODE"] = "1" }
        if let previousExitStatus {
            env["LIGHTTABLE_PREVIOUS_EXIT"] = String(previousExitStatus)
        }
        previousExitStatus = nil
        if env["LIGHTTABLE_CACHE_DIR"] == nil {
            env["LIGHTTABLE_CACHE_DIR"] = cacheDirectory.path
        }
        if env["LIGHTTABLE_PREFS_FILE"] == nil {
            env["LIGHTTABLE_PREFS_FILE"] = supportDirectory
                .appendingPathComponent("prefs.json").path
        }
        if env["LIGHTTABLE_PRESETS_FILE"] == nil {
            env["LIGHTTABLE_PRESETS_FILE"] = supportDirectory
                .appendingPathComponent("presets.json").path
        }
        if env["LIGHTTABLE_AI_DIR"] == nil {
            env["LIGHTTABLE_AI_DIR"] = supportDirectory
                .appendingPathComponent("AI Index", isDirectory: true).path
        }
        let bundledVision = Bundle.main.bundleURL
            .appendingPathComponent("Contents/MacOS/LightTableVision")
        let projectVision = projectDir.appendingPathComponent("build/LightTableVision")
        if env["LIGHTTABLE_VISION_HELPER"] == nil {
            env["LIGHTTABLE_VISION_HELPER"] = FileManager.default
                .isExecutableFile(atPath: bundledVision.path)
                ? bundledVision.path : projectVision.path
        }
        let bundledModels = Bundle.main.resourceURL?
            .appendingPathComponent("models", isDirectory: true)
        if env["LIGHTTABLE_MODEL_DIR"] == nil,
           let bundledModels,
           FileManager.default.fileExists(atPath: bundledModels.path) {
            env["LIGHTTABLE_MODEL_DIR"] = bundledModels.path
        }
        let bundledModules = projectDir.appendingPathComponent(
            "vendor/spektrafilm/src").path
        env["PYTHONPATH"] = [bundledModules, env["PYTHONPATH"]]
            .compactMap { $0 }
            .filter { !$0.isEmpty }
            .joined(separator: ":")
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONPYCACHEPREFIX"] = cacheDirectory
            .appendingPathComponent("python-bytecode").path
        if env["NUMBA_CACHE_DIR"] == nil {
            env["NUMBA_CACHE_DIR"] = cacheDirectory
                .appendingPathComponent("compiled-runtime").path
        }
        env["OMP_NUM_THREADS"] = env["OMP_NUM_THREADS"] ?? "8"
        env["NUMBA_NUM_THREADS"] = "4"
        env["OPENBLAS_NUM_THREADS"] = "4"
        env["PYTHONUNBUFFERED"] = "1"
        env["LIGHTTABLE_LOG_FILE"] = logURL.path
        p.environment = env
        p.standardOutput = log
        p.standardError = log
        p.terminationHandler = { [weak self] exited in
            let status = exited.terminationStatus
            DispatchQueue.main.async {
                guard let self, self.process === exited else { return }
                self.process = nil
                if self.stopping { return }
                self.onUnexpectedExit?(status)
            }
        }
        try p.run()
        process = p
        startedAt = Date()
    }

    func stop() {
        stopping = true
        guard let p = process, p.isRunning else { process = nil; return }
        p.terminate()
        // Give it a moment to close the listening socket before reuse.
        let deadline = Date().addingTimeInterval(2)
        while p.isRunning && Date() < deadline {
            usleep(50_000)
        }
        if p.isRunning { kill(p.processIdentifier, SIGKILL) }
        process = nil
    }

    func readStartupReport() -> StartupReport? {
        guard let url = startupReportURL,
              let data = try? Data(contentsOf: url),
              let object = try? JSONSerialization.jsonObject(with: data)
                as? [String: Any] else { return nil }
        return StartupReport(
            phase: object["phase"] as? String ?? "",
            detail: object["detail"] as? String,
            code: object["code"] as? String,
            hint: object["hint"] as? String,
            port: object["port"] as? Int,
            holder: object["holder"] as? [String: Any])
    }

    func removeStartupReport() {
        if let url = startupReportURL {
            try? FileManager.default.removeItem(at: url)
        }
    }

    /// True when the cheap health endpoint answers and belongs to our child.
    private func healthy(port: Int, pid: Int32?) -> Bool {
        guard port > 0,
              let target = URL(string: "http://127.0.0.1:\(port)/api/health")
        else { return false }
        var request = URLRequest(url: target)
        request.timeoutInterval = 5
        let semaphore = DispatchSemaphore(value: 0)
        var ok = false
        URLSession.shared.dataTask(with: request) { data, response, _ in
            defer { semaphore.signal() }
            guard (response as? HTTPURLResponse)?.statusCode == 200,
                  let data,
                  let object = try? JSONSerialization.jsonObject(with: data)
                    as? [String: Any],
                  object["ok"] as? Bool == true else { return }
            if let pid, let reported = object["pid"] as? Int32, reported != pid {
                return  // another LightTable on this port, not our child
            }
            ok = true
        }.resume()
        _ = semaphore.wait(timeout: .now() + 6)
        return ok
    }

    enum StartupOutcome {
        case ready
        case failed(StartupReport?)
        case exited(Int32, StartupReport?)
        case timedOut(StartupReport?)
    }

    /// Wait for the server, on a background queue, reporting each phase.
    ///
    /// The old probe asked for the whole first library page with a two
    /// second timeout, so a large catalog answering in three seconds read
    /// as "did not start". This one asks the health endpoint, reads the
    /// server's own phase file for progress, adopts a moved port, and
    /// returns the moment the server reports a failure instead of waiting
    /// out the timeout.
    func waitUntilReady(timeout: TimeInterval = 120,
                        progress: @escaping (String) -> Void,
                        completion: @escaping (StartupOutcome) -> Void) {
        let child = process
        let pid = child?.processIdentifier
        DispatchQueue.global(qos: .userInitiated).async {
            let deadline = Date().addingTimeInterval(timeout)
            var lastDetail = ""
            var probePort = self.port
            while Date() < deadline {
                let report = self.readStartupReport()
                if let report {
                    if report.phase == "failed" {
                        DispatchQueue.main.async { completion(.failed(report)) }
                        return
                    }
                    if let moved = report.port, moved > 0 { probePort = moved }
                    if let detail = report.detail, detail != lastDetail {
                        lastDetail = detail
                        DispatchQueue.main.async {
                            let translated: String
                            switch detail {
                            case "Opening the catalog…": translated = L("Opening the catalog…")
                            case "Checking the catalog…": translated = L("Checking the catalog…")
                            case "Starting the local server…": translated = L("Starting the local server…")
                            case "Ready": translated = L("Ready")
                            default: translated = L(detail)
                            }
                            progress(translated)
                        }
                    }
                }
                if let child, !child.isRunning {
                    let status = child.terminationStatus
                    DispatchQueue.main.async {
                        completion(.exited(status, report))
                    }
                    return
                }
                if self.healthy(port: probePort, pid: pid) {
                    let adopted = probePort
                    DispatchQueue.main.async {
                        self.port = adopted
                        completion(.ready)
                    }
                    return
                }
                usleep(250_000)
            }
            let report = self.readStartupReport()
            DispatchQueue.main.async { completion(.timedOut(report)) }
        }
    }
}

// MARK: - Durable edit recovery

// Only hashed identifiers become path components. The browser never supplies
// a file path, and acknowledged revisions cannot remove a newer draft.
final class EditRecoveryStore {
    let root: URL
    init(root: URL) { self.root = root }
    private func identifier(_ value: Any?) throws -> String {
        guard let text = value as? String, text.count == 64,
              text.allSatisfy({ "0123456789abcdef".contains($0) }) else {
            throw NSError(domain: "EditRecovery", code: 1,
                          userInfo: [NSLocalizedDescriptionKey: "Invalid recovery identifier"])
        }
        return text
    }
    func perform(_ body: [String: Any]) throws -> Any {
        let scope = try identifier(body["scope"])
        let directory = root.appendingPathComponent(scope, isDirectory: true)
        let fm = FileManager.default
        let operation = body["operation"] as? String ?? ""
        if operation == "list" {
            guard fm.fileExists(atPath: directory.path) else { return [[String: Any]]() }
            return try fm.contentsOfDirectory(at: directory,
                includingPropertiesForKeys: nil).filter { $0.pathExtension == "json" }
                .map { url -> Any in
                    do { return try JSONSerialization.jsonObject(with: Data(contentsOf: url)) }
                    catch { return ["journalError": error.localizedDescription, "recordKey": url.lastPathComponent] }
                }
        }
        let key = try identifier(body["key"])
        let destination = directory.appendingPathComponent(key + ".json")
        if operation == "remove" {
            guard fm.fileExists(atPath: destination.path) else { return true }
            let previous = try JSONSerialization.jsonObject(with: Data(contentsOf: destination)) as? [String: Any]
            if let token = body["token"] as? String, previous?["token"] as? String == token {
                try fm.removeItem(at: destination)
                try sync(directory)
            }
            return true
        }
        guard operation == "put", let record = body["value"] as? [String: Any],
              record["token"] is String, record["name"] is String,
              record["payload"] is [String: Any] else {
            throw NSError(domain: "EditRecovery", code: 2,
                          userInfo: [NSLocalizedDescriptionKey: "Invalid recovery record"])
        }
        let bytes = try JSONSerialization.data(withJSONObject: record, options: [.sortedKeys])
        guard bytes.count <= 64 * 1024 * 1024 else {
            throw NSError(domain: "EditRecovery", code: 3,
                          userInfo: [NSLocalizedDescriptionKey: "Edit recovery record exceeds 64 MB"])
        }
        try fm.createDirectory(at: directory, withIntermediateDirectories: true)
        // Leave damaged bytes intact for manual recovery rather than silently
        // replacing the only copy during a later edit.
        if fm.fileExists(atPath: destination.path) {
            _ = try JSONSerialization.jsonObject(with: Data(contentsOf: destination))
        }
        try bytes.write(to: destination, options: [.atomic])
        try sync(destination)
        try sync(directory)
        return true
    }
    private func sync(_ url: URL) throws {
        let descriptor = open(url.path, O_RDONLY)
        guard descriptor >= 0 else { throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno)) }
        defer { close(descriptor) }
        guard fsync(descriptor) == 0 else { throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno)) }
    }
}

// MARK: - Original Photos library import

/// One resource at a time, streamed to disk. Photos is only read; completed
/// copies are atomically promoted to deterministic paths so a retry adds new
/// originals without duplicating those already imported.
private final class PhotosLibraryImporter {
    private struct Item {
        let resource: PHAssetResource
        let identity: String
    }
    private final class Transfer {
        let temporary: URL
        let destination: URL
        let handle: FileHandle
        var request: PHAssetResourceDataRequestID?
        var error: Error?
        init(temporary: URL, destination: URL, handle: FileHandle) {
            self.temporary = temporary
            self.destination = destination
            self.handle = handle
        }
    }
    private let directory: URL
    private let event: ([String: Any]) -> Void
    private let queue = DispatchQueue(label: "lighttable.photos-library-import", qos: .utility)
    private let cancellationLock = NSLock()
    private var cancelled = false
    private var items: [Item] = []
    private var index = 0
    private var imported = 0
    private var existing = 0
    private var failures = 0
    private var transfer: Transfer?
    private var finished = false
    private var lockDescriptor: Int32 = -1
    private var lastProgressTime = Date.distantPast

    init(directory: URL, event: @escaping ([String: Any]) -> Void) {
        self.directory = directory
        self.event = event
    }

    private var isCancelled: Bool {
        cancellationLock.lock()
        defer { cancellationLock.unlock() }
        return cancelled
    }

    func start() {
        queue.async { [self] in
            guard !isCancelled else { finish("cancelled"); return }
            lockDescriptor = Darwin.open(directory.appendingPathComponent(
                ".lighttable-library-import.lock").path, O_CREAT | O_WRONLY, S_IRUSR | S_IWUSR)
            guard lockDescriptor >= 0, flock(lockDescriptor, LOCK_EX | LOCK_NB) == 0 else {
                finish("error", message: L("Another LightTable window may be importing this library. Close that import and try again."))
                return
            }
            emit("running", message: L("Reading your Photos library…"))
            let options = PHFetchOptions()
            options.includeHiddenAssets = true
            options.includeAllBurstAssets = true
            let assets = PHAsset.fetchAssets(with: .image, options: options)
            assets.enumerateObjects { asset, _, stop in
                if self.isCancelled { stop.pointee = true; return }
                autoreleasepool {
                    // Do not import adjusted versions or the video part of a
                    // Live Photo. A RAW+JPEG pair has two original resources.
                    let originals = PHAssetResource.assetResources(for: asset)
                        .filter { $0.type == .photo || $0.type == .alternatePhoto }
                    var occurrences: [String: Int] = [:]
                    for resource in originals {
                        let key = "\(asset.localIdentifier)|\(resource.type.rawValue)|\(resource.uniformTypeIdentifier)|\(resource.originalFilename)"
                        let occurrence = occurrences[key, default: 0]
                        occurrences[key] = occurrence + 1
                        self.items.append(Item(resource: resource, identity: "\(key)|\(occurrence)"))
                    }
                }
            }
            guard !isCancelled else { finish("cancelled"); return }
            emit("running", message: L("Copying original photos…"))
            next()
        }
    }

    func cancel() {
        cancellationLock.lock()
        cancelled = true
        cancellationLock.unlock()
        queue.async { [self] in
            guard !finished else { return }
            discardTransfer()
            finish("cancelled")
        }
    }

    /// Quit cannot wait for a download, but it must close and remove its partial
    /// file. The directory lock and completed copies remain crash-safe too.
    func shutdown() {
        cancellationLock.lock()
        cancelled = true
        cancellationLock.unlock()
        queue.sync {
            discardTransfer()
            finish("cancelled")
        }
    }

    private static func digest(_ value: String) -> String {
        SHA256.hash(data: Data(value.utf8)).map { String(format: "%02x", $0) }.joined()
    }

    private func destination(for item: Item) -> URL {
        let digest = Self.digest(item.identity)
        let name = URL(fileURLWithPath: item.resource.originalFilename)
        let invalid = CharacterSet(charactersIn: "/:\0").union(.controlCharacters)
        let stem = name.deletingPathExtension().lastPathComponent
            .components(separatedBy: invalid).joined(separator: "_")
        var safeStem = String((stem.isEmpty ? "Photo" : stem).prefix(80))
        while safeStem.utf8.count > 100 { safeStem.removeLast() }
        let ext = name.pathExtension.isEmpty
            ? (UTType(item.resource.uniformTypeIdentifier)?.preferredFilenameExtension ?? "")
            : name.pathExtension
        let safeExtension = String(ext.components(separatedBy: invalid).joined(separator: "_").prefix(16))
        return directory.appendingPathComponent("Originals", isDirectory: true)
            .appendingPathComponent(String(digest.prefix(2)), isDirectory: true)
            .appendingPathComponent("\(safeStem)-\(digest)" + (safeExtension.isEmpty ? "" : ".\(safeExtension)"))
    }

    private func next() {
        guard !finished else { return }
        guard !isCancelled else { finish("cancelled"); return }
        guard index < items.count else { finish("completed"); return }
        let item = items[index]
        let destination = destination(for: item)
        let parent = destination.deletingLastPathComponent()
        // Each deterministic destination is only created after the whole
        // resource has been flushed. A partial file never counts as imported.
        if let values = try? destination.resourceValues(forKeys: [.isRegularFileKey, .fileSizeKey]),
           values.isRegularFile == true, (values.fileSize ?? 0) > 0 {
            existing += 1
            advance()
            return
        }
        do {
            try FileManager.default.createDirectory(at: parent, withIntermediateDirectories: true)
            let temporary = parent.appendingPathComponent(".lighttable-\(Self.digest(item.identity)).partial")
            if FileManager.default.fileExists(atPath: temporary.path) {
                try FileManager.default.removeItem(at: temporary)
            }
            guard FileManager.default.createFile(atPath: temporary.path, contents: nil) else {
                throw CocoaError(.fileWriteUnknown)
            }
            let current = Transfer(temporary: temporary, destination: destination,
                                   handle: try FileHandle(forWritingTo: temporary))
            transfer = current
            let options = PHAssetResourceRequestOptions()
            options.isNetworkAccessAllowed = true
            options.progressHandler = { [weak self, weak current] progress in
                guard let self, let current else { return }
                self.queue.async {
                    guard self.transfer === current, !self.finished else { return }
                    self.emit("running", message: L("Downloading an original from iCloud…"), progress: progress)
                }
            }
            current.request = PHAssetResourceManager.default().requestData(
                for: item.resource, options: options,
                dataReceivedHandler: { [weak self, weak current] data in
                    guard let self, let current else { return }
                    // Synchronous backpressure keeps at most one PhotoKit data
                    // chunk pending, even when disk is slower than the download.
                    self.queue.sync {
                        guard self.transfer === current, current.error == nil else { return }
                        do { try current.handle.write(contentsOf: data) }
                        catch { current.error = error }
                    }
                }, completionHandler: { [weak self, weak current] error in
                    guard let self, let current else { return }
                    self.queue.async {
                        guard self.transfer === current, !self.finished else { return }
                        self.complete(current, error: error)
                    }
                })
        } catch {
            failures += 1
            advance()
        }
    }

    private func complete(_ current: Transfer, error: Error?) {
        if isCancelled { discardTransfer(); finish("cancelled"); return }
        do {
            if let error = current.error ?? error { throw error }
            try current.handle.synchronize()
            try current.handle.close()
            let values = try current.temporary.resourceValues(forKeys: [.fileSizeKey])
            guard (values.fileSize ?? 0) > 0 else { throw CocoaError(.fileReadCorruptFile) }
            // moveItem does not overwrite a user's existing file.
            try FileManager.default.moveItem(at: current.temporary, to: current.destination)
            imported += 1
        } catch {
            try? current.handle.close()
            try? FileManager.default.removeItem(at: current.temporary)
            failures += 1
        }
        transfer = nil
        advance()
    }

    private func advance() {
        index += 1
        emit("running", message: L("Copying original photos…"))
        // Yield between resources so cancellation is handled even when a large
        // rerun consists entirely of already imported files.
        queue.async { [self] in next() }
    }

    private func discardTransfer() {
        guard let current = transfer else { return }
        transfer = nil
        if let request = current.request { PHAssetResourceManager.default().cancelDataRequest(request) }
        try? current.handle.close()
        try? FileManager.default.removeItem(at: current.temporary)
    }

    private func finish(_ state: String, message: String? = nil) {
        guard !finished else { return }
        finished = true
        if lockDescriptor >= 0 {
            _ = flock(lockDescriptor, LOCK_UN)
            Darwin.close(lockDescriptor)
            lockDescriptor = -1
        }
        emit(state, message: message ?? (state == "cancelled"
            ? L("Import stopped. Completed copies are safe; run it again to continue.")
            : (items.isEmpty ? L("No photo originals were found in your Photos library.")
                : (failures > 0 ? L("Import finished with some originals unavailable. Run it again to retry.")
                    : L("Your photo originals are ready in LightTable.")))))
        items.removeAll()
    }

    private func emit(_ state: String, message: String, progress: Double? = nil) {
        let now = Date()
        if state == "running", now.timeIntervalSince(lastProgressTime) < 0.2 { return }
        lastProgressTime = now
        var payload: [String: Any] = [
            "type": "photosLibraryImport", "state": state, "completed": index,
            "total": items.count, "imported": imported, "existing": existing,
            "failures": failures, "message": message, "path": directory.path,
        ]
        if let progress { payload["resourceProgress"] = progress }
        DispatchQueue.main.async { [event] in event(payload) }
    }
}

// MARK: - Preset links

/// A link names a reviewed catalog entry; it is never a file or fetch URL.
/// Keep the ASCII grammar identical to the Windows host and catalog IDs.
func presetID(from raw: String) -> String? {
    let prefix = "lighttable://preset/"
    guard raw.utf8.count <= prefix.utf8.count + 129,
          raw.hasPrefix(prefix) else { return nil }
    let identifier = String(raw.dropFirst(prefix.count))
    let parts = identifier.split(separator: "/", omittingEmptySubsequences: false)
    guard parts.count == 2 else { return nil }
    for part in parts {
        let bytes = Array(part.utf8)
        guard (1...64).contains(bytes.count),
              let first = bytes.first,
              (first >= 97 && first <= 122) || (first >= 48 && first <= 57),
              bytes.allSatisfy({ ($0 >= 97 && $0 <= 122) || ($0 >= 48 && $0 <= 57) || $0 == 45 })
        else { return nil }
    }
    return identifier
}

/// Decode an exported recipe or its submission ZIP before opening the same
/// user-controlled Save dialog. The bridge never supplies a destination path.
func presetExportData(content: String, encoding: String = "utf8") throws -> Data {
    guard content.utf8.count <= 15 * 1024 * 1024 else {
        throw NSError(domain: "LightTablePresetExport", code: 1,
            userInfo: [NSLocalizedDescriptionKey: "The preset export exceeds 15 MB."])
    }
    switch encoding {
    case "utf8", "utf-8":
        return Data(content.utf8)
    case "base64":
        guard let data = Data(base64Encoded: content),
              data.base64EncodedString() == content else {
            throw NSError(domain: "LightTablePresetExport", code: 2,
                userInfo: [NSLocalizedDescriptionKey: "The preset export contains invalid base64 data."])
        }
        return data
    default:
        throw NSError(domain: "LightTablePresetExport", code: 3,
            userInfo: [NSLocalizedDescriptionKey: "The preset export uses an unsupported encoding."])
    }
}

// MARK: - App

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate,
                         WKScriptMessageHandler, NSMenuItemValidation,
                         NSWindowDelegate,
                         PHPickerViewControllerDelegate {
#if canImport(Sparkle)
    private let updaterController = SPUStandardUpdaterController(
        startingUpdater: true, updaterDelegate: nil, userDriverDelegate: nil)
#endif
    private let editRecoveryQueue = DispatchQueue(label: "lighttable.edit-recovery", qos: .userInitiated)
    private var closePending = false
    private var closeApproved = false
    var window: NSWindow!
    var webView: WKWebView!
    private var secondaryLoupeWindow: NSWindow?
    var nativePreview: NativePreviewRenderer?
    let nativePerfLogQueue = DispatchQueue(label: "lighttable.native-perf-log")
    let server = ServerController()
    var folder: String = ""
    var sources: [FolderSource] = []
    private var editorMenuState: [String: Any] = [:]
    private var editorCommandItems: [NSMenuItem] = []
    private var schemeCommandItems: [String: NSMenuItem] = [:]
    private var photoPicker: PHPickerViewController?
    private var pendingPhotosImportEvent: [String: Any]?
    private var photosLibraryImporter: PhotosLibraryImporter?
    private var photosLibraryImportEvent: [String: Any]?
    private var photosLibraryAuthorizationID: UUID?
    private var firstRun = false
    private var pendingSetupFolderEvent: [String: Any]?
    private var pendingSetupCatalogEvent: [String: Any]?
    private var pendingPresetLinks: [String] = []
    private var presetLinksReady = false
    private let photoImportQueue = DispatchQueue(
        label: "lighttable.photos-import", qos: .userInitiated)
    /// Consecutive unexpected server exits. Reset after a session that ran
    /// long enough to count as healthy.
    private var crashRestarts = 0
    private static let crashRestartLimit = 3
    private static let healthySessionSeconds: TimeInterval = 300
    private static let startupTimeout: TimeInterval = 120

    func applicationDidFinishLaunching(_ note: Notification) {
        guard chooseInitialLanguage() else { return }
        buildMenu()
        buildWindow()

        guard server.isInstalled else {
            showFatal(L("LightTable's Python runtime was not found at:\n{path}\n\nExpected bundled Python and server.py resources, or a project checkout with .venv/bin/python. Rebuild or restore the app.", ["path": projectDir.path]))
            return
        }

        let defaults = UserDefaults.standard
        let environmentFolder = ProcessInfo.processInfo.environment[
            "LIGHTTABLE_DIR"]?.trimmingCharacters(in: .whitespacesAndNewlines)
        firstRun = environmentFolder?.isEmpty != false
            && defaults.object(forKey: "photoFolder") == nil
            && defaults.object(forKey: "folderSources") == nil
            && defaults.integer(forKey: "firstRunCompleted") < 1
        sources = firstRun ? [] : loadSources()
        if firstRun {
            do { folder = try gettingStartedFolder().path }
            catch { showFatal(L("Could not prepare your library: {error}", ["error": error.localizedDescription])); return }
        } else {
            folder = (environmentFolder?.isEmpty == false ? environmentFolder : nil)
                ?? defaults.string(forKey: "photoFolder")
                ?? sources.first?.path ?? defaultPhotoFolder
        }
        var isDir: ObjCBool = false
        if !FileManager.default.fileExists(atPath: folder, isDirectory: &isDir)
            || !isDir.boolValue {
            if let picked = pickFolder(title: L("Choose a photo folder")) { folder = picked } else {
                showFatal(L("No photo folder chosen.")); return
            }
        }
        launch(folder: folder)
    }

    func application(_ application: NSApplication, open urls: [URL]) {
        for url in urls.prefix(8) {
            guard let id = presetID(from: url.absoluteString) else { continue }
            // Bound the startup queue; repeated links after readiness still work.
            if pendingPresetLinks.count == 8 { pendingPresetLinks.removeFirst() }
            pendingPresetLinks.append(id)
        }
        guard !pendingPresetLinks.isEmpty else { return }
        window?.makeKeyAndOrderFront(nil)
        application.activate(ignoringOtherApps: true)
        deliverPresetLinks()
    }

    private func deliverPresetLinks() {
        guard presetLinksReady, webView != nil else { return }
        let links = pendingPresetLinks
        pendingPresetLinks.removeAll()
        for id in links { sendEvent(["type": "presetLink", "id": id]) }
    }

    func applicationWillTerminate(_ note: Notification) {
        photosLibraryImporter?.shutdown()
        server.stop()
    }

    /// Complete this before the folder picker or server can write preferences.
    private func chooseInitialLanguage() -> Bool {
        do {
            let preferences = try nativeLocalization.preferences()
            if preferences["localeChosen"] as? Bool == true {
                try nativeLocalization.reload()
                return true
            }
        } catch {
            showFatal(L("Your language preference could not be read. The preferences file was left unchanged.") + "\n\n" + error.localizedDescription)
            return false
        }
        let primary = Locale.preferredLanguages.first ?? "en"
        var selected = NativeLocaleStore.isEnglish(primary) ? "en" : nativeLocalization.suggested(for: primary)
        nativeLocalization.setLocale(selected)
        if !NativeLocaleStore.isEnglish(primary) {
            NSApp.activate(ignoringOtherApps: true)
            let alert = NSAlert()
            alert.messageText = L("Choose your language")
            alert.informativeText = L("You can change the language later in Settings.")
            let picker = NSPopUpButton(frame: NSRect(x: 0, y: 0, width: 320, height: 28), pullsDown: false)
            picker.addItems(withTitles: nativeLocalization.languages.map { $0.nativeName })
            picker.selectItem(at: nativeLocalization.languages.firstIndex { $0.code == selected } ?? 0)
            picker.setAccessibilityLabel(L("Language"))
            alert.accessoryView = picker
            alert.addButton(withTitle: L("Continue"))
            alert.addButton(withTitle: L("Quit"))
            guard alert.runModal() == .alertFirstButtonReturn else { NSApp.terminate(nil); return false }
            selected = nativeLocalization.languages[max(0, picker.indexOfSelectedItem)].code
        }
        // This is a user-driven retry, not a background loop. Keep the choice
        // selected and never start the server until persistence succeeds.
        var retry = true
        while retry {
            do {
                try nativeLocalization.saveChoice(selected)
                return true
            } catch {
                nativeLocalization.setLocale(selected)
                let alert = NSAlert()
                alert.messageText = L("Your language choice could not be saved")
                alert.informativeText = L("Your selection is retained. Retry saving or quit; LightTable has not started and your existing preferences were left unchanged.") + "\n\n" + error.localizedDescription
                alert.addButton(withTitle: L("Try Again"))
                alert.addButton(withTitle: L("Quit"))
                retry = alert.runModal() == .alertFirstButtonReturn
            }
        }
        NSApp.terminate(nil)
        return false
    }


    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if closeApproved || webView == nil { return .terminateNow }
        prepareToClose { approved in sender.reply(toApplicationShouldTerminate: approved) }
        return .terminateLater
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        if sender !== window || closeApproved { return true }
        prepareToClose { [weak self] approved in
            if approved { self?.window.performClose(nil) }
        }
        return false
    }

    private func prepareToClose(completion: @escaping (Bool) -> Void) {
        guard !closePending else { completion(false); return }
        closePending = true
        window.contentView?.isHidden = false
        var finished = false
        let finish: (Bool) -> Void = { [weak self] saved in
            guard let self, !finished else { return }
            finished = true
            self.closePending = false
            if saved {
                self.closeApproved = true
                completion(true)
                return
            }
            let alert = NSAlert()
            alert.messageText = L("Some edits have not been saved")
            alert.informativeText = L("Keep this window open and retry saving. If you quit, LightTable will offer any available local recovery the next time this catalog opens.")
            alert.addButton(withTitle: L("Keep Open"))
            alert.addButton(withTitle: L("Quit Anyway"))
            let approved = alert.runModal() == .alertSecondButtonReturn
            self.closeApproved = approved
            if !approved { self.sendEvent(["type": "closeCancelled"]) }
            completion(approved)
        }
        webView.callAsyncJavaScript(
            "return window.lightTablePrepareToClose ? await window.lightTablePrepareToClose() : true",
            arguments: [:], in: nil, in: .page) { result in
                if case .success(let value) = result { finish(value as? Bool == true) }
                else { finish(false) }
            }
        DispatchQueue.main.asyncAfter(deadline: .now() + 12) { finish(false) }
    }

    func applicationShouldTerminateAfterLastWindowClosed(
        _ app: NSApplication) -> Bool { true }

    func applicationSupportsSecureRestorableState(
        _ app: NSApplication) -> Bool { true }

    // MARK: UI

    private func buildWindow() {
        if ProcessInfo.processInfo.environment[
            "LIGHTTABLE_DISABLE_NATIVE_PREVIEW"] != "1" {
            nativePreview = NativePreviewRenderer()
        }
        nativePreview?.onInteractionPresented = { [weak self] sample in
            self?.sendEvent(["type": "nativeInteractionPresented", "sample": sample])
        }
        let cfg = WKWebViewConfiguration()
        cfg.websiteDataStore = .nonPersistent()
        cfg.userContentController.add(self, name: "lightTable")
        let benchmarkRequested = ProcessInfo.processInfo.environment[
            "LIGHTTABLE_NATIVE_BENCHMARK_OUTPUT"] != nil
        var nativeBootstrap = """
            document.documentElement.classList.add('native-shell');
            document.documentElement.style.setProperty(
                '--native-window-controls-w',
                '\(Int(WindowChrome.trafficLightClearance))px');
            """
        if let encoded = try? JSONEncoder().encode(Locale.preferredLanguages),
           let json = String(data: encoded, encoding: .utf8) {
            nativeBootstrap += "window.__LIGHTTABLE_SYSTEM_LANGUAGES__=\(json);"
        }
        if nativePreview != nil {
            nativeBootstrap += "window.__LIGHTTABLE_NATIVE_PREVIEW__=true;" +
                "document.documentElement.classList.add('native-preview-shell');"
        }
        if let width = Int(ProcessInfo.processInfo.environment[
            "LIGHTTABLE_NATIVE_BENCHMARK_WIDTH"] ?? ""),
           (320...10_000).contains(width) {
            nativeBootstrap +=
                "window.__LIGHTTABLE_NATIVE_BENCHMARK_WIDTH__=\(width);"
        }
        if let image = ProcessInfo.processInfo.environment[
            "LIGHTTABLE_NATIVE_BENCHMARK_IMAGE"], !image.isEmpty,
           let encoded = try? JSONEncoder().encode(image),
           let json = String(data: encoded, encoding: .utf8) {
            nativeBootstrap +=
                "window.__LIGHTTABLE_NATIVE_BENCHMARK_IMAGE__=\(json);"
        }
        if benchmarkRequested {
            if ProcessInfo.processInfo.environment["LIGHTTABLE_INTERACTION_BENCHMARK"] == "1" {
                nativeBootstrap += "window.__LIGHTTABLE_INTERACTION_BENCHMARK__=true;"
            }
            let iterations = min(100, max(1, Int(
                ProcessInfo.processInfo.environment[
                    "LIGHTTABLE_NATIVE_BENCHMARK_ITERATIONS"] ?? "30") ?? 30))
            nativeBootstrap +=
                "window.__LIGHTTABLE_BENCHMARK__=true;" +
                "window.__LIGHTTABLE_NATIVE_BENCHMARK_ITERATIONS__=\(iterations);"
            if ProcessInfo.processInfo.environment[
                "LIGHTTABLE_NATIVE_SMOKE_JOURNEY"] == "1" {
                nativeBootstrap += "window.__LIGHTTABLE_NATIVE_SMOKE_JOURNEY__=true;"
            }
            if let layer = ProcessInfo.processInfo.environment[
                "LIGHTTABLE_NATIVE_JOURNEY_LAYER"],
               ["pr", "package", "raw-curated", "raw-full"].contains(layer),
               let encoded = try? JSONEncoder().encode(layer),
               let json = String(data: encoded, encoding: .utf8) {
                nativeBootstrap +=
                    "window.__LIGHTTABLE_NATIVE_JOURNEY_LAYER__=\(json);"
            }
            if let destination = ProcessInfo.processInfo.environment[
                "LIGHTTABLE_NATIVE_JOURNEY_EXPORT_DIR"], !destination.isEmpty,
               let encoded = try? JSONEncoder().encode(destination),
               let json = String(data: encoded, encoding: .utf8) {
                nativeBootstrap +=
                    "window.__LIGHTTABLE_NATIVE_JOURNEY_EXPORT_DIR__=\(json);"
            }
            if let rawNames = ProcessInfo.processInfo.environment[
                "LIGHTTABLE_NATIVE_JOURNEY_IMAGES"],
               let data = rawNames.data(using: .utf8),
               let names = try? JSONDecoder().decode([String].self, from: data),
               let encoded = try? JSONEncoder().encode(names),
               let json = String(data: encoded, encoding: .utf8) {
                nativeBootstrap +=
                    "window.__LIGHTTABLE_NATIVE_JOURNEY_IMAGES__=\(json);"
            }
            if let corrupt = ProcessInfo.processInfo.environment[
                "LIGHTTABLE_NATIVE_JOURNEY_CORRUPT"], !corrupt.isEmpty,
               let encoded = try? JSONEncoder().encode(corrupt),
               let json = String(data: encoded, encoding: .utf8) {
                nativeBootstrap +=
                    "window.__LIGHTTABLE_NATIVE_JOURNEY_CORRUPT__=\(json);"
            }
        }
        cfg.userContentController.addUserScript(WKUserScript(
            source: nativeBootstrap,
            injectionTime: .atDocumentStart,
            forMainFrameOnly: true))
        webView = LightTableWebView(frame: .zero, configuration: cfg)
        webView.navigationDelegate = self
        webView.setValue(false, forKey: "drawsBackground")
        // The page handles pinch itself; don't let WebKit scale the whole UI.
        webView.allowsMagnification = false

        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1500, height: 950),
            styleMask: [.titled, .closable, .miniaturizable, .resizable,
                        .fullSizeContentView],
            backing: .buffered, defer: false)
        window.title = "LightTable"
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.titlebarSeparatorStyle = .none
        window.delegate = self
        window.backgroundColor = NSColor(calibratedWhite: 0.125, alpha: 1)
        let root = NSView(frame: window.contentLayoutRect)
        root.wantsLayer = true
        root.layer?.backgroundColor = NSColor(
            calibratedWhite: 0.125, alpha: 1).cgColor
        if let previewView = nativePreview?.view {
            root.addSubview(previewView)
        }
        webView.frame = root.bounds
        webView.autoresizingMask = [.width, .height]
        root.addSubview(webView)
        let controller = NSViewController()
        controller.view = root
        window.contentViewController = controller
        if !benchmarkRequested {
            window.setFrameAutosaveName("LightTableWindow")
        }
        window.minSize = NSSize(width: 1100, height: 700)
        window.center()
        window.makeKeyAndOrderFront(nil)
        layoutTrafficLights()
        NSApp.activate(ignoringOtherApps: true)
        showSplash(L("Starting LightTable…"))
    }

    private func layoutTrafficLights() {
        let buttons = [NSWindow.ButtonType.closeButton, .miniaturizeButton,
                       .zoomButton].compactMap(window.standardWindowButton)
        guard buttons.count == 3,
              let titlebarView = buttons.first?.superview,
              buttons.allSatisfy({ $0.superview === titlebarView }) else { return }

        guard !window.styleMask.contains(.fullScreen) else { return }
        let centerFromTop = WindowChrome.topBarHeight / 2
        let center = titlebarView.convert(
            NSPoint(x: 0, y: window.frame.height - centerFromTop), from: nil)
        for (button, nativeFrame) in zip(buttons, WindowChrome.nativeTrafficLightFrames) {
            var frame = nativeFrame
            frame.origin.y = center.y - frame.height / 2
            if button.frame != frame { button.frame = frame }
        }
    }

    func windowDidUpdate(_ notification: Notification) {
        guard notification.object as? NSWindow === window else { return }
        // AppKit can restore its title-bar frames after resize/key-window layout.
        layoutTrafficLights()
    }

    func windowDidResize(_ notification: Notification) {
        guard notification.object as? NSWindow === window else { return }
        layoutTrafficLights()
    }

    func windowWillClose(_ notification: Notification) {
        if notification.object as? NSWindow === window {
            secondaryLoupeWindow?.close()
        } else if notification.object as? NSWindow === secondaryLoupeWindow {
            secondaryLoupeWindow = nil
        }
    }

    private func openSecondaryLoupe() {
        if let loupeWindow = secondaryLoupeWindow {
            loupeWindow.deminiaturize(nil)
            loupeWindow.makeKeyAndOrderFront(nil)
            return
        }
        guard let pageURL = webView.url,
              let url = URL(string: "/web/loupe.html", relativeTo: pageURL)?.absoluteURL
        else { return }

        let configuration = WKWebViewConfiguration()
        // BroadcastChannel requires the same session as the editor, including
        // its nonpersistent data store. Do not inject the editor's Metal bridge.
        configuration.websiteDataStore = webView.configuration.websiteDataStore
        let loupeView = WKWebView(frame: .zero, configuration: configuration)
        loupeView.autoresizingMask = [.width, .height]
        loupeView.allowsMagnification = false

        let screen = NSScreen.screens.first { $0 != window.screen }
            ?? window.screen ?? NSScreen.main
        let available = screen?.visibleFrame
            ?? NSRect(x: 0, y: 0, width: 1200, height: 800)
        let width = min(1200, available.width)
        let height = min(800, available.height - 40)
        let loupeWindow = NSWindow(
            contentRect: NSRect(x: available.midX - width / 2,
                                y: available.midY - height / 2,
                                width: width, height: height),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered, defer: false)
        loupeWindow.title = L("LightTable — Secondary Loupe")
        loupeWindow.minSize = NSSize(width: 480, height: 320)
        loupeWindow.isReleasedWhenClosed = false
        loupeWindow.delegate = self
        loupeWindow.contentView = loupeView
        secondaryLoupeWindow = loupeWindow
        loupeWindow.makeKeyAndOrderFront(nil)
        loupeView.load(URLRequest(url: url))
    }

    func windowDidExitFullScreen(_ notification: Notification) {
        guard notification.object as? NSWindow === window else { return }
        layoutTrafficLights()
    }

    private func showSplash(_ message: String) {
        let message = message.replacingOccurrences(of: "&", with: "&amp;")
            .replacingOccurrences(of: "<", with: "&lt;")
            .replacingOccurrences(of: ">", with: "&gt;")
        let direction = nativeLocalization.languages.first { $0.code == nativeLocalization.locale }?.dir ?? "ltr"
        webView.loadHTMLString("""
            <html lang="\(nativeLocalization.locale)" dir="\(direction)"><head><meta charset="utf-8"><style>
            html,body{height:100%;margin:0;background:#171717;color:#c8c8c8;
              font:13px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
              display:flex;align-items:center;justify-content:center}
            .b{width:20px;height:20px;border:2px solid #3a3a3a;border-top-color:#4b9cf5;
              border-radius:50%;animation:s .7s linear infinite;margin-inline-end:12px}
            @keyframes s{to{transform:rotate(360deg)}}
            </style></head><body><div class="b"></div>\(message)</body></html>
            """, baseURL: nil)
    }

    private func showFatal(_ message: String) {
        let a = NSAlert()
        a.messageText = "LightTable"
        a.informativeText = message
        a.alertStyle = .critical
        a.addButton(withTitle: L("Quit"))
        a.runModal()
        NSApp.terminate(nil)
    }

    private func launch(folder: String) {
        let clean = normalized(folder)
        guard isDirectory(clean) else {
            sendEvent(["type": "error", "message": L("That folder is no longer available.")])
            return
        }
        self.folder = clean
        nativePreview?.hide()
        let automated = ProcessInfo.processInfo.environment[
            "LIGHTTABLE_DIR"]?.isEmpty == false
        if !automated && !(firstRun && sources.isEmpty) {
            addSource(clean)
            UserDefaults.standard.set(clean, forKey: "photoFolder")
        }
        window.title = L("LightTable — {name}", ["name": (clean as NSString).lastPathComponent])
            + (server.safeMode ? L(" (Safe Mode)") : "")
        showSplash(server.safeMode ? L("Starting LightTable in Safe Mode…")
                                   : L("Starting LightTable…"))
        server.onUnexpectedExit = { [weak self] status in
            self?.serverExited(status: status)
        }
        do {
            try server.start(folder: clean)
        } catch {
            presentStartupFailure(
                summary: L("Could not start the render server.\n\n{error}", ["error": String(describing: error)]),
                report: nil)
            return
        }
        server.waitUntilReady(
            timeout: Self.startupTimeout,
            progress: { [weak self] detail in self?.showSplash(detail) }
        ) { [weak self] outcome in
            guard let self else { return }
            switch outcome {
            case .ready:
                self.server.removeStartupReport()
                let url = URL(string: "http://127.0.0.1:\(self.server.port)/")!
                self.webView.load(URLRequest(url: url))
            case .failed(let report):
                self.presentStartupFailure(
                    summary: report?.detail.map { L($0) } ?? L("The server reported a failure."),
                    report: report)
            case .exited(let status, let report):
                self.presentStartupFailure(
                    summary: L("The server exited with status {status} before it was ready.", ["status": String(status)]), report: report)
            case .timedOut(let report):
                self.presentStartupFailure(
                    summary: L("The server did not answer within {seconds} seconds. It may still be checking a very large library, or it may be stuck.\n\nLast reported step: {step}.", ["seconds": String(Int(Self.startupTimeout)), "step": report?.detail.map { L($0) } ?? L("unknown")]),
                    report: report)
            }
        }
    }

    /// Replace the dead splash with a specific reason and real choices.
    private func presentStartupFailure(summary: String,
                                       report: ServerController.StartupReport?) {
        showSplash(L("LightTable could not start — see Help ▸ Diagnostics ▸ Show Server Log"))
        server.removeStartupReport()
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = L("LightTable could not start")
        var text = summary
        if let hint = report?.hint, !hint.isEmpty { text += "\n\n\(hint)" }
        text += "\n\n" + L("The server log has the details.")
        alert.informativeText = text
        if report?.code == "catalog-locked", let holder = report?.holder,
           let holderPid = holder["pid"] as? Int32 {
            let place = (holder["headless"] as? Bool == true)
                ? L("a command-line server") : L("another LightTable window")
            let owner = (holder["port"] as? Int).map {
                L("process {pid}, port {port}", ["pid": String(holderPid), "port": String($0)])
            } ?? L("process {pid}", ["pid": String(holderPid)])
            alert.informativeText = summary + "\n\n" + L("The library is open in {place} ({owner}). Quit it and try again, or let LightTable quit it for you.", ["place": place, "owner": owner]) + "\n\n" + L("The server log has the details.")
            alert.addButton(withTitle: L("Quit the Other Copy and Retry"))
            alert.addButton(withTitle: L("Try Again"))
            alert.addButton(withTitle: L("Show Server Log"))
            alert.addButton(withTitle: L("Quit"))
            switch alert.runModal() {
            case .alertFirstButtonReturn:
                kill(holderPid, SIGTERM)
                showSplash(L("Waiting for the other copy to quit…"))
                DispatchQueue.main.asyncAfter(deadline: .now() + 2.5) {
                    [weak self] in
                    guard let self else { return }
                    self.launch(folder: self.folder)
                }
            case .alertSecondButtonReturn:
                launch(folder: folder)
            case .alertThirdButtonReturn:
                showLog(nil)
                presentStartupFailure(summary: summary, report: report)
            default:
                NSApp.terminate(nil)
            }
            return
        }
        alert.addButton(withTitle: L("Try Again"))
        alert.addButton(withTitle: server.safeMode ? L("Try Again in Safe Mode")
                                                   : L("Start in Safe Mode"))
        alert.addButton(withTitle: L("Show Server Log"))
        alert.addButton(withTitle: L("Quit"))
        switch alert.runModal() {
        case .alertFirstButtonReturn:
            launch(folder: folder)
        case .alertSecondButtonReturn:
            server.safeMode = true
            launch(folder: folder)
        case .alertThirdButtonReturn:
            showLog(nil)
            presentStartupFailure(summary: summary, report: report)
        default:
            NSApp.terminate(nil)
        }
    }

    /// The server went away while the window was in use.
    private func serverExited(status: Int32) {
        nativePreview?.hide()
        if status == ServerController.restartExitStatus {
            // Asked for: the catalog was replaced, or a restart was requested.
            showSplash(L("Restarting LightTable…"))
            launch(folder: resolvedLaunchFolder())
            return
        }
        if server.uptime > Self.healthySessionSeconds { crashRestarts = 0 }
        crashRestarts += 1
        server.previousExitStatus = status
        if crashRestarts <= Self.crashRestartLimit {
            showSplash(L("LightTable's engine stopped unexpectedly (status {status}). Restarting…", ["status": String(status)]))
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { [weak self] in
                guard let self else { return }
                self.launch(folder: self.resolvedLaunchFolder())
            }
            return
        }
        showSplash(L("LightTable's engine keeps stopping — see Help ▸ Diagnostics ▸ Show Server Log"))
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = L("LightTable's engine keeps stopping")
        alert.informativeText = L("It has stopped {count} times in a row (last status {status}). If the same photo was open each time, the next launch sets it aside automatically. Safe Mode opens the library with background services off so you can use Library Health to look for the cause.\n\nThe server log has the details.", ["count": String(crashRestarts), "status": String(status)])
        alert.addButton(withTitle: L("Start in Safe Mode"))
        alert.addButton(withTitle: L("Try Again"))
        alert.addButton(withTitle: L("Show Server Log"))
        alert.addButton(withTitle: L("Quit"))
        switch alert.runModal() {
        case .alertFirstButtonReturn:
            server.safeMode = true
            crashRestarts = 0
            launch(folder: resolvedLaunchFolder())
        case .alertSecondButtonReturn:
            crashRestarts = 0
            launch(folder: resolvedLaunchFolder())
        case .alertThirdButtonReturn:
            showLog(nil)
            serverExited(status: status)
        default:
            NSApp.terminate(nil)
        }
    }

    /// The folder to relaunch with: the current one if it still exists,
    /// otherwise the first source that does, otherwise Pictures.
    private func resolvedLaunchFolder() -> String {
        if isDirectory(folder) { return folder }
        if let existing = sources.first(where: { isDirectory($0.path) }) {
            return existing.path
        }
        return defaultPhotoFolder
    }

    @objc func restartInSafeMode(_ sender: Any?) {
        server.safeMode = true
        launch(folder: resolvedLaunchFolder())
    }

    @objc func openLibraryHealth(_ sender: Any?) {
        sendEvent(["type": "openLibraryHealth"])
    }

    @objc func openRecoveryFolder(_ sender: Any?) {
        let directory = server.catalogDirectory
        let recovery = directory.appendingPathComponent("Recovery", isDirectory: true)
        let target = FileManager.default.fileExists(atPath: recovery.path)
            ? recovery : directory
        try? FileManager.default.createDirectory(
            at: target, withIntermediateDirectories: true)
        NSWorkspace.shared.open(target)
    }

    private func pickFolder(title: String) -> String? {
        let panel = NSOpenPanel()
        panel.title = title
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        if !folder.isEmpty {
            panel.directoryURL = URL(fileURLWithPath: folder)
        }
        return panel.runModal() == .OK ? panel.url?.path : nil
    }

    private func pickPhotoSources() -> [String] {
        let panel = NSOpenPanel()
        panel.title = L("Add photos to LightTable")
        panel.prompt = L("Add Photos")
        panel.message = L("Choose photos or folders. LightTable keeps originals in place.")
        panel.canChooseDirectories = true
        panel.canChooseFiles = true
        panel.allowsMultipleSelection = true
        let photoTypes = photoExtensions().compactMap {
            UTType(filenameExtension: $0)
        }
        // A damaged development checkout should still be able to choose a
        // file. The server remains the final format validator.
        panel.allowedContentTypes = photoTypes.isEmpty
            ? [.image, .movie, .data] : photoTypes
        if !folder.isEmpty { panel.directoryURL = URL(fileURLWithPath: folder) }
        guard panel.runModal() == .OK else { return [] }
        var seen = Set<String>()
        return panel.urls.compactMap { url in
            var isDir: ObjCBool = false
            FileManager.default.fileExists(atPath: url.path, isDirectory: &isDir)
            let path = normalized(isDir.boolValue ? url.path : url.deletingLastPathComponent().path)
            return seen.insert(path).inserted ? path : nil
        }
    }

    private func gettingStartedFolder() throws -> URL {
        let root = FileManager.default.urls(for: .applicationSupportDirectory,
                                             in: .userDomainMask).first!
        let destination = root.appendingPathComponent("LightTable", isDirectory: true)
            .appendingPathComponent("Getting Started", isDirectory: true)
        try FileManager.default.createDirectory(at: destination, withIntermediateDirectories: true)
        return destination
    }

    private func finishFirstRun() {
        firstRun = false
        pendingSetupFolderEvent = nil
        pendingSetupCatalogEvent = nil
        if photosLibraryImportEvent?["state"] as? String != "running" {
            photosLibraryImportEvent = nil
        }
        let defaults = UserDefaults.standard
        defaults.set(1, forKey: "firstRunCompleted")
        // Skip keeps the empty workspace on the next launch too.
        defaults.set(folder, forKey: "photoFolder")
        saveSources()
        sendEvent(sourcePayload())
    }

    private func completeSetupCatalogImport(_ body: [String: Any]) {
        var seen = Set<String>()
        let paths = (body["paths"] as? [String] ?? []).compactMap { path -> String? in
            guard (path as NSString).isAbsolutePath else { return nil }
            let clean = normalized(path)
            guard isDirectory(clean), seen.insert(clean).inserted else { return nil }
            return clean
        }
        paths.forEach { addSource($0) }
        pendingSetupCatalogEvent = [
            "type": "setupCatalogImported",
            "matched": max(0, body["matched"] as? Int ?? 0),
            "unmatched": max(0, body["unmatched"] as? Int ?? 0),
        ]
        if let first = paths.first {
            launch(folder: first)
        } else if let pendingSetupCatalogEvent {
            sendEvent(pendingSetupCatalogEvent)
        }
    }

    private func publishPhotosLibraryEvent(_ payload: [String: Any]) {
        photosLibraryImportEvent = payload
        sendEvent(payload)
    }

    private func importEntirePhotosLibrary() {
        guard photosLibraryImporter == nil, photosLibraryAuthorizationID == nil else {
            if let photosLibraryImportEvent { sendEvent(photosLibraryImportEvent) }
            return
        }
        let authorizationID = UUID()
        photosLibraryAuthorizationID = authorizationID
        publishPhotosLibraryEvent([
            "type": "photosLibraryImport", "state": "running", "completed": 0,
            "total": 0, "imported": 0, "existing": 0, "failures": 0,
            "message": L("Waiting for permission to read your Photos library…"),
        ])
        // The OS permission request is only reached by the explicit import
        // action, never by startup, capability checks, or selecting the card.
        PHPhotoLibrary.requestAuthorization(for: .readWrite) { [weak self] status in
            DispatchQueue.main.async {
                guard let self, self.photosLibraryAuthorizationID == authorizationID else { return }
                self.photosLibraryAuthorizationID = nil
                guard status == .authorized else {
                    self.publishPhotosLibraryEvent([
                        "type": "photosLibraryImport", "state": "error", "completed": 0,
                        "total": 0, "imported": 0, "existing": 0, "failures": 0,
                        "message": status == .limited
                            ? L("Importing the entire library needs full Photos access. Allow access in System Settings, or choose individual photos instead.")
                            : L("Photos access was not allowed. You can enable it in System Settings → Privacy & Security → Photos, or start with a folder."),
                    ])
                    return
                }
                do {
                    let directory = try self.photosImportRoot()
                    let importer = PhotosLibraryImporter(directory: directory) { [weak self] payload in
                        guard let self else { return }
                        self.publishPhotosLibraryEvent(payload)
                        guard let state = payload["state"] as? String, state != "running" else { return }
                        self.photosLibraryImporter = nil
                        let available = (payload["imported"] as? Int ?? 0) + (payload["existing"] as? Int ?? 0)
                        if available > 0 {
                            self.addSource(directory.path)
                            self.sendEvent(self.sourcePayload())
                            self.launch(folder: directory.path)
                        }
                    }
                    self.photosLibraryImporter = importer
                    importer.start()
                } catch {
                    self.publishPhotosLibraryEvent([
                        "type": "photosLibraryImport", "state": "error", "completed": 0,
                        "total": 0, "imported": 0, "existing": 0, "failures": 0,
                        "message": L("Could not create the Photos import folder: {error}", ["error": error.localizedDescription]),
                    ])
                }
            }
        }
    }

    private func cancelEntirePhotosLibraryImport() {
        if photosLibraryAuthorizationID != nil {
            photosLibraryAuthorizationID = nil
            publishPhotosLibraryEvent([
                "type": "photosLibraryImport", "state": "cancelled", "completed": 0,
                "total": 0, "imported": 0, "existing": 0, "failures": 0,
                "message": "Import cancelled. No photos were copied.",
            ])
        }
        photosLibraryImporter?.cancel()
    }

    private func replaySetupEvents() {
        if let photosLibraryImportEvent { sendEvent(photosLibraryImportEvent) }
        if let pendingSetupFolderEvent { sendEvent(pendingSetupFolderEvent) }
        if let pendingSetupCatalogEvent { sendEvent(pendingSetupCatalogEvent) }
    }

    private func presentPhotosPicker() {
        let alert = NSAlert()
        alert.messageText = L("Import from Apple Photos")
        alert.informativeText = L("Current format preserves the asset's present representation, including RAW or embedded depth when available. Compatible creates a broadly readable image. Selected files are copied into {path}.", ["path": "Pictures/LightTable Imports/Apple Photos"])
        alert.addButton(withTitle: L("Current Format"))
        alert.addButton(withTitle: L("Compatible"))
        alert.addButton(withTitle: L("Cancel"))
        let response = alert.runModal()
        guard response == .alertFirstButtonReturn
                || response == .alertSecondButtonReturn else { return }
        var configuration = PHPickerConfiguration()
        configuration.filter = .images
        configuration.selectionLimit = 0
        configuration.selection = .ordered
        configuration.preferredAssetRepresentationMode =
            response == .alertFirstButtonReturn ? .current : .compatible
        let picker = PHPickerViewController(configuration: configuration)
        picker.delegate = self
        photoPicker = picker
        window.contentViewController?.presentAsSheet(picker)
    }

    private func photosImportRoot() throws -> URL {
        guard let pictures = FileManager.default.urls(
            for: .picturesDirectory, in: .userDomainMask).first else {
            throw CocoaError(.fileNoSuchFile)
        }
        let destination = pictures
            .appendingPathComponent("LightTable Imports", isDirectory: true)
            .appendingPathComponent("Apple Photos", isDirectory: true)
        try FileManager.default.createDirectory(
            at: destination, withIntermediateDirectories: true)
        return destination
    }

    private func importedPhotoDestination(
        in directory: URL, provider: NSItemProvider, temporaryURL: URL,
        typeIdentifier: String
    ) -> URL {
        let fallbackExtension = UTType(typeIdentifier)?.preferredFilenameExtension
            ?? temporaryURL.pathExtension
        let suggested = (provider.suggestedName ?? temporaryURL.lastPathComponent)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        let supplied = URL(fileURLWithPath: suggested)
        let rawStem = supplied.deletingPathExtension().lastPathComponent
        let invalid = CharacterSet(charactersIn: "/:\0")
            .union(.controlCharacters)
        let stem = rawStem.components(separatedBy: invalid).joined(separator: "_")
        let safeStem = stem.isEmpty ? "Photo" : stem
        let ext = supplied.pathExtension.isEmpty
            ? fallbackExtension : supplied.pathExtension
        let suffix = ext.isEmpty ? "" : ".\(ext)"
        var candidate = directory.appendingPathComponent(safeStem + suffix)
        var sequence = 2
        while FileManager.default.fileExists(atPath: candidate.path) {
            candidate = directory.appendingPathComponent(
                "\(safeStem)-\(sequence)\(suffix)")
            sequence += 1
        }
        return candidate
    }

    func picker(_ picker: PHPickerViewController,
                didFinishPicking results: [PHPickerResult]) {
        picker.dismiss(nil)
        photoPicker = nil
        guard !results.isEmpty else { return }
        let directory: URL
        do {
            directory = try photosImportRoot()
        } catch {
            sendEvent(["type": "error",
                       "message": L("Could not create the Photos import folder: {error}", ["error": error.localizedDescription])])
            return
        }
        let group = DispatchGroup()
        var imported = 0
        var failures = 0
        var unsupported = 0
        for result in results {
            let provider = result.itemProvider
            let identifiers = provider.registeredTypeIdentifiers.filter {
                UTType($0)?.conforms(to: .image) == true
            }
            let identifier = identifiers.first {
                UTType($0)?.conforms(to: .rawImage) == true
            } ?? identifiers.first
            guard let identifier else { unsupported += 1; continue }
            group.enter()
            provider.loadFileRepresentation(forTypeIdentifier: identifier) {
                [weak self] temporaryURL, _ in
                guard let self else { group.leave(); return }
                self.photoImportQueue.sync {
                    defer { group.leave() }
                    guard let temporaryURL else { failures += 1; return }
                    do {
                        let destination = self.importedPhotoDestination(
                            in: directory, provider: provider,
                            temporaryURL: temporaryURL,
                            typeIdentifier: identifier)
                        try FileManager.default.copyItem(
                            at: temporaryURL, to: destination)
                        imported += 1
                    } catch {
                        failures += 1
                    }
                }
            }
        }
        group.notify(queue: .main) { [weak self] in
            guard let self else { return }
            guard imported > 0 else {
                self.sendEvent(["type": "error",
                                "message": L("No Apple Photos assets could be imported.")])
                return
            }
            self.pendingPhotosImportEvent = [
                "type": "photosImported", "count": imported,
                "failures": failures + unsupported, "path": directory.path,
            ]
            self.addSource(directory.path)
            self.launch(folder: directory.path)
        }
    }

    private func photoExtensions() -> [String] {
        let candidates = [
            projectDir.appendingPathComponent("media-formats.json"),
            Bundle.main.resourceURL?.appendingPathComponent("media-formats.json"),
        ].compactMap { $0 }
        for url in candidates where FileManager.default.fileExists(atPath: url.path) {
            guard let data = try? Data(contentsOf: url),
                  let groups = try? JSONSerialization.jsonObject(with: data)
                    as? [String: [String]] else { continue }
            return ["raw", "processed", "video"].flatMap { groups[$0] ?? [] }
        }
        return []
    }

    private func normalized(_ path: String) -> String {
        URL(fileURLWithPath: path).standardizedFileURL.path
    }

    private func isDirectory(_ path: String) -> Bool {
        var isDir: ObjCBool = false
        return FileManager.default.fileExists(atPath: path, isDirectory: &isDir)
            && isDir.boolValue
    }

    private func loadSources() -> [FolderSource] {
        if let data = UserDefaults.standard.data(forKey: "folderSources"),
           let saved = try? JSONDecoder().decode([FolderSource].self, from: data) {
            return saved
        }
        if let old = UserDefaults.standard.string(forKey: "photoFolder") {
            return [FolderSource(path: normalized(old), favorite: true)]
        }
        return [FolderSource(path: normalized(defaultPhotoFolder), favorite: true)]
    }

    private func saveSources() {
        if let data = try? JSONEncoder().encode(sources) {
            UserDefaults.standard.set(data, forKey: "folderSources")
        }
    }

    private func addSource(_ path: String, favorite: Bool = false) {
        let clean = normalized(path)
        if !sources.contains(where: { $0.path == clean }) {
            sources.append(FolderSource(path: clean,
                                        favorite: sources.isEmpty ? true : favorite))
            sources.sort { $0.path.localizedCaseInsensitiveCompare($1.path) == .orderedAscending }
            saveSources()
        }
    }

    private func sourcePayload() -> [String: Any] {
        [
            "type": "sources",
            "active": folder,
            "firstRun": firstRun,
            "photosLibraryImportAvailable": true,
            "sources": sources.map { source in
                ["path": source.path,
                 "name": (source.path as NSString).lastPathComponent,
                 "favorite": source.favorite,
                 "available": isDirectory(source.path)] as [String: Any]
            },
        ]
    }

    private func sendEvent(_ payload: [String: Any]) {
        guard JSONSerialization.isValidJSONObject(payload),
              let data = try? JSONSerialization.data(withJSONObject: payload),
              let json = String(data: data, encoding: .utf8),
              webView != nil else { return }
        DispatchQueue.main.async { [weak self] in
            self?.webView.evaluateJavaScript("window.lightTableNativeEvent?.(\(json))")
        }
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        sendEvent(sourcePayload())
        replaySetupEvents()
        if let pendingPhotosImportEvent {
            sendEvent(pendingPhotosImportEvent)
            self.pendingPhotosImportEvent = nil
        }
        sendEvent(["type": "requestMenuState"])
    }

    func webView(_ webView: WKWebView,
                 didStartProvisionalNavigation navigation: WKNavigation!) {
        (webView as? LightTableWebView)?.resetWindowChromeLayout()
        // A server restart or library switch may change the editor's origin.
        if webView === self.webView {
            presetLinksReady = false
            secondaryLoupeWindow?.close()
        }
    }

    /// A catalog file from another editor, opened read-only by the server.
    func pickCatalogFile() -> String? {
        let panel = NSOpenPanel()
        panel.title = L("Choose a catalog")
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        panel.allowedContentTypes = ["lrcat", "lrcat-data", "cocatalog"]
            .compactMap { UTType(filenameExtension: $0) }
        panel.allowsOtherFileTypes = true
        guard panel.runModal() == .OK, let url = panel.url else { return nil }
        return url.path
    }

    /// Cards and other removable volumes, for the ingest dialog.
    func removableVolumes() -> [[String: Any]] {
        let keys: [URLResourceKey] = [.volumeNameKey, .volumeIsRemovableKey,
                                      .volumeIsEjectableKey,
                                      .volumeTotalCapacityKey]
        let urls = FileManager.default.mountedVolumeURLs(
            includingResourceValuesForKeys: keys,
            options: [.skipHiddenVolumes]) ?? []
        return urls.compactMap { url in
            guard let values = try? url.resourceValues(forKeys: Set(keys)),
                  (values.volumeIsRemovable == true
                   || values.volumeIsEjectable == true) else { return nil }
            return ["path": url.path,
                    "name": values.volumeName ?? url.lastPathComponent,
                    "capacity": values.volumeTotalCapacity ?? 0]
        }
    }

    func userContentController(_ userContentController: WKUserContentController,
                               didReceive message: WKScriptMessage) {
        guard message.name == "lightTable",
              let body = message.body as? [String: Any],
              let action = body["action"] as? String else { return }
        switch action {
        case "requestPresetLinks":
            guard message.frameInfo.isMainFrame,
                  message.frameInfo.request.url?.host == "127.0.0.1",
                  message.frameInfo.request.url?.port == Int(server.port) else { return }
            presetLinksReady = true
            deliverPresetLinks()
        case "editJournal":
            guard message.frameInfo.isMainFrame,
                  message.frameInfo.request.url?.host == "127.0.0.1",
                  message.frameInfo.request.url?.port == Int(server.port),
                  let id = body["id"] as? String else { return }
            let directory = server.catalogDirectory.appendingPathComponent("Recovery/EditDrafts", isDirectory: true)
            editRecoveryQueue.async { [weak self] in
                var result: [String: Any] = ["type": "editJournalReply", "id": id]
                do { result["result"] = try EditRecoveryStore(root: directory).perform(body) }
                catch { result["error"] = error.localizedDescription }
                let reply = result
                DispatchQueue.main.async { self?.sendEvent(reply) }
            }
        case "openSecondaryLoupe":
            openSecondaryLoupe()
        case "requestSources":
            sendEvent(sourcePayload())
            replaySetupEvents()
        case "completeFirstRun":
            finishFirstRun()
        case "setupCatalogImported":
            completeSetupCatalogImport(body)
        case "setupChooseFolder":
            if let picked = pickFolder(title: L("Choose your first photo folder")) {
                addSource(picked)
                pendingSetupFolderEvent = ["type": "setupFolderSelected", "path": picked]
                launch(folder: picked)
            } else {
                sendEvent(["type": "setupFolderCancelled"])
            }
        case "importApplePhotosLibrary":
            importEntirePhotosLibrary()
        case "cancelApplePhotosLibraryImport":
            cancelEntirePhotosLibraryImport()
        case "showServerLog":
            showLog(nil)
        case "localizationChanged":
            // The page sends this only after /api/prefs confirms the write.
            // Read that same file; never trust a locale passed over the bridge.
            do {
                try nativeLocalization.reload()
                buildMenu()
                secondaryLoupeWindow?.title = L("LightTable — Secondary Loupe")
            } catch {
                sendEvent(["type": "error", "message": L("The saved language preference could not be read.")])
            }
        case "openRecoveryFolder":
            openRecoveryFolder(nil)
        case "restartServer":
            restartServer(nil)
        case "restartInSafeMode":
            restartInSafeMode(nil)
        case "addPhotos":
            let paths = pickPhotoSources()
            paths.forEach { addSource($0) }
            if let first = paths.first { launch(folder: first) }
        case "importApplePhotos":
            presentPhotosPicker()
        case "addFolder":
            if let picked = pickFolder(title: L("Add a folder to LightTable")) {
                addSource(picked)
                launch(folder: picked)
            }
        case "chooseExportFolder":
            if let picked = pickFolder(title: L("Choose an export destination")) {
                sendEvent(["type": "exportFolderSelected", "path": picked])
            }
        case "chooseCatalogFile":
            if let picked = pickCatalogFile() {
                sendEvent(["type": "catalogFileSelected", "path": picked])
            }
        case "chooseIngestFolder":
            let field = (body["field"] as? String) ?? "ingestSource"
            if let picked = pickFolder(title: L("Choose a card or folder")) {
                sendEvent(["type": "ingestFolderSelected",
                           "field": field, "path": picked])
            }
        case "choosePreferenceFolder":
            let key = (body["key"] as? String) ?? ""
            if let picked = pickFolder(title: L("Choose a settings folder")) {
                sendEvent(["type": "preferenceFolderSelected",
                           "key": key, "path": picked])
            }
        case "listVolumes":
            sendEvent(["type": "volumes", "volumes": removableVolumes()])
        case "listEditors":
            let probe = FileManager.default.temporaryDirectory
                .appendingPathComponent("LightTable-External-Edit.tif")
            var seen = Set<String>()
            let editors: [[String: Any]] = NSWorkspace.shared
                .urlsForApplications(toOpen: probe).compactMap { url in
                    guard seen.insert(url.path).inserted else { return nil }
                    let name = (Bundle(url: url)?.object(
                        forInfoDictionaryKey: "CFBundleDisplayName") as? String)
                        ?? url.deletingPathExtension().lastPathComponent
                    return ["name": name, "path": url.path]
                }
            sendEvent(["type": "editors", "editors": editors])
        case "openWith":
            guard let paths = body["paths"] as? [String], !paths.isEmpty else { return }
            let urls = paths.map { URL(fileURLWithPath: $0) }
            let app = (body["app"] as? String) ?? ""
            if app.isEmpty {
                urls.forEach { NSWorkspace.shared.open($0) }
            } else {
                let configuration = NSWorkspace.OpenConfiguration()
                configuration.activates = true
                NSWorkspace.shared.open(
                    urls, withApplicationAt: URL(fileURLWithPath: app),
                    configuration: configuration) { _, error in
                        if let error {
                            self.sendEvent(["type": "error",
                                            "message": error.localizedDescription])
                        }
                    }
            }
        case "trashFiles":
            // Deletion always goes to the Trash through the platform, never an
            // unlink: a mistaken cull has to be recoverable in the Finder.
            guard let paths = body["paths"] as? [String] else { return }
            var trashed = 0
            for path in paths {
                do {
                    try FileManager.default.trashItem(
                        at: URL(fileURLWithPath: path), resultingItemURL: nil)
                    trashed += 1
                } catch {
                    continue
                }
            }
            sendEvent(["type": "trashed", "count": trashed])
        case "selectSource":
            if let path = body["path"] as? String { launch(folder: path) }
        case "toggleFavorite":
            guard let path = body["path"] as? String,
                  let index = sources.firstIndex(where: { $0.path == normalized(path) }) else { return }
            sources[index].favorite.toggle()
            saveSources()
            sendEvent(sourcePayload())
        case "removeSource":
            guard let path = body["path"] as? String, sources.count > 1 else { return }
            let clean = normalized(path)
            sources.removeAll { $0.path == clean }
            saveSources()
            if clean == folder, let next = sources.first(where: { isDirectory($0.path) }) {
                launch(folder: next.path)
            } else {
                sendEvent(sourcePayload())
            }
        case "revealFolder":
            guard let path = body["path"] as? String else { return }
            NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: path)])
        case "renameRoot":
            guard let path = body["path"] as? String,
                  let rawName = body["name"] as? String else { return }
            renameRoot(path: path, name: rawName)
        case "refresh":
            webView.reload()
        case "savePreset":
            guard let filename = body["filename"] as? String,
                  let content = body["content"] as? String else { return }
            savePreset(filename: filename, content: content,
                       encoding: body["encoding"] as? String ?? "utf8")
        case "importPresets":
            importPresets()
        case "menuState":
            editorMenuState = body["state"] as? [String: Any] ?? [:]
            updateEditorMenuItems()
        case "windowChromeLayout":
            (webView as? LightTableWebView)?.updateWindowChromeLayout(body)
        case "nativeViewportLayout":
            updateNativeViewport(body)
        case "nativePreview":
            presentNativePreview(body)
        case "nativeNavigate":
            if let generation = body["generation"] as? Int {
                nativePreview?.beginNavigation(generation: generation)
            }
        case "nativePreloadReset":
            nativePreview?.cancelPreloads(epoch: body["epoch"] as? Int)
        case "nativePreload":
            if let payload = body["surface"] as? [String: Any],
               let pageURL = webView.url,
               let surface = NativeSurfaceDescription(payload: payload, baseURL: pageURL),
               let epoch = body["epoch"] as? Int {
                nativePreview?.preload(surface, epoch: epoch)
            }
        case "nativeGrade":
            nativePreview?.recordInteraction(body["interaction"] as? [String: Any])
            if let grade = body["grade"] as? [String: Any] {
                nativePreview?.updateGrade(
                    grade,
                    softProof: body["softProof"] as? [String: Any] ?? [:],
                    updateCurves: body["curvesChanged"] as? Bool ?? true)
            }
        case "nativeMasks":
            nativePreview?.updateMasks(body)
        case "nativeEdits":
            nativePreview?.updateEdits(
                optics: body["optics"] as? [String: Any] ?? [:],
                heals: body["heals"] as? [[String: Any]] ?? [])
        case "nativeCompare":
            if let position = (body["position"] as? NSNumber)?.doubleValue {
                nativePreview?.updateComparePosition(position)
            }
        case "nativeReference":
            nativePreview?.updateReference(body)
        case "viewerAppearance":
            if let value = body["color"] as? String,
               let color = Self.color(hex: value) {
                window.backgroundColor = color
                window.contentViewController?.view.layer?.backgroundColor = color.cgColor
                nativePreview?.setBackgroundColor(color)
            }
        case "automaticUpdateChecks":
            if let enabled = body["enabled"] as? Bool {
                UserDefaults.standard.set(enabled, forKey: "SUEnableAutomaticChecks")
            }
        case "requestNotificationPermission":
            UNUserNotificationCenter.current().requestAuthorization(
                options: [.alert, .sound]) { _, _ in }
        case "notify":
            let content = UNMutableNotificationContent()
            content.title = (body["title"] as? String) ?? "LightTable"
            content.body = (body["message"] as? String) ?? L("Job complete")
            content.sound = .default
            UNUserNotificationCenter.current().add(
                UNNotificationRequest(identifier: UUID().uuidString,
                                      content: content, trigger: nil))
        case "nativeBenchmarkComplete":
            finishNativeBenchmark(body)
        case "nativeBenchmarkProgress":
            appendNativePerf(body)
        default:
            break
        }
    }

    private static func color(hex: String) -> NSColor? {
        let value = hex.trimmingCharacters(in: .whitespacesAndNewlines)
            .trimmingCharacters(in: CharacterSet(charactersIn: "#"))
        guard value.count == 6, let rgb = Int(value, radix: 16) else { return nil }
        return NSColor(
            calibratedRed: CGFloat((rgb >> 16) & 0xff) / 255,
            green: CGFloat((rgb >> 8) & 0xff) / 255,
            blue: CGFloat(rgb & 0xff) / 255,
            alpha: 1)
    }

    private func importPresets() {
        let panel = NSOpenPanel()
        panel.title = L("Import Presets")
        panel.prompt = L("Import")
        panel.message = L("Choose preset files, a ZIP bundle, or a folder of presets.")
        panel.canChooseDirectories = true
        panel.canChooseFiles = true
        panel.allowsMultipleSelection = true
        let extensions = [
            "ltpreset", "rrpreset", "xmp", "lrtemplate", "costyle",
            "costylepack", "zip", "json",
        ]
        panel.allowedContentTypes = extensions.compactMap {
            UTType(filenameExtension: $0)
        }
        let cameraRawSettings = URL(fileURLWithPath: NSHomeDirectory())
            .appendingPathComponent("Library/Application Support/Adobe/CameraRaw/Settings")
        if FileManager.default.fileExists(atPath: cameraRawSettings.path) {
            panel.directoryURL = cameraRawSettings
        }
        guard panel.runModal() == .OK else { return }

        let allowed = Set(extensions)
        let selected = panel.urls
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let manager = FileManager.default
            var urls: [URL] = []
            for url in selected {
                var isDirectory: ObjCBool = false
                guard manager.fileExists(atPath: url.path, isDirectory: &isDirectory) else {
                    continue
                }
                if !isDirectory.boolValue {
                    if allowed.contains(url.pathExtension.lowercased()) { urls.append(url) }
                    continue
                }
                let keys: [URLResourceKey] = [.isRegularFileKey, .fileSizeKey]
                let options: FileManager.DirectoryEnumerationOptions = [
                    .skipsHiddenFiles, .skipsPackageDescendants,
                ]
                let children = manager.enumerator(
                    at: url, includingPropertiesForKeys: keys,
                    options: options)
                while let child = children?.nextObject() as? URL {
                    if allowed.contains(child.pathExtension.lowercased()) {
                        urls.append(child)
                    }
                }
            }

            var seen = Set<String>()
            urls = urls
                .filter { seen.insert($0.standardizedFileURL.path).inserted }
                .sorted { $0.path.localizedCaseInsensitiveCompare($1.path) == .orderedAscending }

            let maxFiles = 250
            let maxTotalBytes = 25 * 1024 * 1024
            var totalBytes = 0
            var files: [[String: String]] = []
            var failures: [[String: String]] = []
            if urls.count > maxFiles {
                failures.append(["name": "selection", "error": L("more than 250 preset files")])
            }
            for url in urls.prefix(maxFiles) {
                do {
                    let data = try Data(contentsOf: url, options: [.mappedIfSafe])
                    guard totalBytes + data.count <= maxTotalBytes else {
                        failures.append(["name": url.lastPathComponent,
                                         "error": L("selection is larger than 25 MB")])
                        continue
                    }
                    totalBytes += data.count
                    files.append(["name": url.lastPathComponent,
                                  "base64": data.base64EncodedString()])
                } catch {
                    failures.append(["name": url.lastPathComponent,
                                     "error": L("file could not be read")])
                }
            }
            DispatchQueue.main.async {
                self?.sendEvent(["type": "presetFilesSelected",
                                 "files": files, "failures": failures])
            }
        }
    }

    private func savePreset(filename: String, content: String, encoding: String = "utf8") {
        let data: Data
        do { data = try presetExportData(content: content, encoding: encoding) }
        catch {
            sendEvent(["type": "error",
                       "message": L("Could not export preset: {error}", ["error": error.localizedDescription])])
            return
        }
        let cleanName = (filename as NSString).lastPathComponent
        let panel = NSSavePanel()
        panel.title = L("Export Preset")
        panel.prompt = L("Export")
        panel.nameFieldStringValue = cleanName
        if let type = UTType(filenameExtension: (cleanName as NSString).pathExtension) {
            panel.allowedContentTypes = [type]
        }
        guard panel.runModal() == .OK, let destination = panel.url else { return }
        do {
            try data.write(to: destination, options: .atomic)
            sendEvent(["type": "presetSaved", "filename": destination.lastPathComponent])
        } catch {
            sendEvent(["type": "error",
                       "message": L("Could not export preset: {error}", ["error": error.localizedDescription])])
        }
    }

    private func updateNativeViewport(_ body: [String: Any]) {
        guard let preview = nativePreview,
              let canvas = body["canvas"] as? [String: Any],
              let clip = body["clip"] as? [String: Any],
              let canvasRect = browserRect(canvas),
              let clipRect = browserRect(clip)
        else {
            nativePreview?.hide()
            return
        }
        let visible = (body["visible"] as? Bool ?? false)
        let clipped = canvasRect.intersection(clipRect)
        guard visible, !clipped.isNull, clipped.width >= 1, clipped.height >= 1 else {
            preview.hide()
            return
        }
        let appKitFrame = NSRect(
            x: clipped.minX,
            y: webView.bounds.height - clipped.maxY,
            width: clipped.width,
            height: clipped.height)
        let uvScale = SIMD2<Float>(
            Float(clipped.width / canvasRect.width),
            Float(clipped.height / canvasRect.height))
        let uvOffset = SIMD2<Float>(
            Float((clipped.minX - canvasRect.minX) / canvasRect.width),
            Float((clipped.minY - canvasRect.minY) / canvasRect.height))
        preview.setFrame(
            appKitFrame, uvScale: uvScale, uvOffset: uvOffset, visible: true)
    }

    private func browserRect(_ payload: [String: Any]) -> NSRect? {
        func value(_ key: String) -> Double? {
            (payload[key] as? NSNumber)?.doubleValue
        }
        guard let x = value("x"), let y = value("y"),
              let width = value("width"), let height = value("height"),
              width > 0, height > 0 else { return nil }
        return NSRect(x: x, y: y, width: width, height: height)
    }

    private func presentNativePreview(_ body: [String: Any]) {
        guard let preview = nativePreview,
              let surfacePayload = body["surface"] as? [String: Any],
              let pageURL = webView.url,
              let surface = NativeSurfaceDescription(
                payload: surfacePayload, baseURL: pageURL),
              let generation = body["generation"] as? Int
        else { return }
        let grade = body["grade"] as? [String: Any] ?? [:]
        preview.load(surface, generation: generation, grade: grade, prepare: { [weak self] in
            self?.updateNativeViewport(body)
            preview.updateEdits(
                optics: body["optics"] as? [String: Any] ?? [:],
                heals: body["heals"] as? [[String: Any]] ?? [])
            if let masks = body["masks"] as? [String: Any] {
                preview.updateMasks(masks)
            }
            if let originalPayload = body["original"] as? [String: Any],
               let original = NativeSurfaceDescription(payload: originalPayload, baseURL: pageURL) {
                preview.loadOriginal(original, generation: generation)
            } else { preview.clearOriginal() }
        }        ) { [weak self] result in
            switch result {
            case .success(let timings):
                self?.recordNativePreview(
                    generation: generation, surface: surface, timings: timings)
                self?.sendEvent([
                    "type": "nativePreviewPresented",
                    "generation": generation,
                    "timings": timings.payload,
                ])
            case .failure(let error):
                self?.recordNativePreviewFailure(
                    generation: generation, surface: surface, error: error)
                self?.sendEvent([
                    "type": "nativePreviewFailed",
                    "generation": generation,
                    "message": error.localizedDescription,
                ])
            }
        }
    }

    private func recordNativePreview(
        generation: Int,
        surface: NativeSurfaceDescription,
        timings: NativePreviewTimings
    ) {
        var payload = timings.payload
        payload["generation"] = generation
        payload["width"] = surface.width
        payload["height"] = surface.height
        payload["format"] = surface.format
        appendNativePerf(payload)
    }

    private func recordNativePreviewFailure(
        generation: Int,
        surface: NativeSurfaceDescription,
        error: Error
    ) {
        appendNativePerf([
            "generation": generation,
            "width": surface.width,
            "height": surface.height,
            "format": surface.format,
            "error": error.localizedDescription,
        ])
    }

    private func appendNativePerf(_ payload: [String: Any]) {
        guard let path = ProcessInfo.processInfo.environment[
            "LIGHTTABLE_NATIVE_PERF_LOG"], !path.isEmpty,
              JSONSerialization.isValidJSONObject(payload),
              var data = try? JSONSerialization.data(withJSONObject: payload)
        else { return }
        data.append(0x0a)
        nativePerfLogQueue.async {
            let url = URL(fileURLWithPath: path)
            if !FileManager.default.fileExists(atPath: path) {
                FileManager.default.createFile(atPath: path, contents: nil)
            }
            guard let handle = try? FileHandle(forWritingTo: url) else { return }
            defer { try? handle.close() }
            do {
                try handle.seekToEnd()
                try handle.write(contentsOf: data)
            } catch {
                return
            }
        }
    }

    private func finishNativeBenchmark(_ payload: [String: Any]) {
        var enriched = payload
        enriched["windowConnected"] = window?.isVisible == true
            && webView?.window === window
        guard let screenshotPath = ProcessInfo.processInfo.environment[
            "LIGHTTABLE_NATIVE_JOURNEY_SCREENSHOT"], !screenshotPath.isEmpty else {
            writeNativeBenchmark(enriched)
            return
        }
        captureBenchmarkScreenshot(at: URL(fileURLWithPath: screenshotPath)) {
            [weak self] result in
            var completed = enriched
            completed["screenshot"] = result
            self?.writeNativeBenchmark(completed)
        }
    }

    private func captureBenchmarkScreenshot(
        at url: URL,
        completion: @escaping ([String: Any]) -> Void
    ) {
        guard let contentView = window?.contentView,
              let webView,
              let nativeImage = nativePreview?.snapshot()
        else {
            completion(["error": "The native preview was unavailable for capture."])
            return
        }
        let configuration = WKSnapshotConfiguration()
        configuration.rect = webView.bounds
        configuration.afterScreenUpdates = true
        webView.takeSnapshot(with: configuration) { [weak self] webImage, error in
            guard let self, let webImage else {
                completion(["error": error?.localizedDescription
                    ?? "The web interface could not be captured."])
                return
            }
            let pointSize = contentView.bounds.size
            let scale = max(1, self.window.backingScaleFactor)
            let pixelWidth = max(1, Int((pointSize.width * scale).rounded()))
            let pixelHeight = max(1, Int((pointSize.height * scale).rounded()))
            guard let bitmap = NSBitmapImageRep(
                bitmapDataPlanes: nil,
                pixelsWide: pixelWidth,
                pixelsHigh: pixelHeight,
                bitsPerSample: 8,
                samplesPerPixel: 4,
                hasAlpha: true,
                isPlanar: false,
                colorSpaceName: .deviceRGB,
                bytesPerRow: 0,
                bitsPerPixel: 0),
                  let context = NSGraphicsContext(bitmapImageRep: bitmap)
            else {
                completion(["error": "The screenshot bitmap could not be created."])
                return
            }
            bitmap.size = pointSize
            NSGraphicsContext.saveGraphicsState()
            NSGraphicsContext.current = context
            context.cgContext.scaleBy(x: scale, y: scale)
            NSColor(calibratedWhite: 0.0627, alpha: 1).setFill()
            NSBezierPath.fill(contentView.bounds)
            if let frame = self.nativePreview?.view.frame {
                nativeImage.draw(
                    in: frame,
                    from: .zero,
                    operation: .sourceOver,
                    fraction: 1)
            }
            webImage.draw(
                in: contentView.bounds,
                from: .zero,
                operation: .sourceOver,
                fraction: 1)
            context.flushGraphics()
            NSGraphicsContext.restoreGraphicsState()
            guard let png = bitmap.representation(using: .png, properties: [:]) else {
                completion(["error": "The screenshot could not be encoded as PNG."])
                return
            }
            do {
                try FileManager.default.createDirectory(
                    at: url.deletingLastPathComponent(),
                    withIntermediateDirectories: true)
                try png.write(to: url, options: .atomic)
                let frame = self.nativePreview?.view.frame ?? .zero
                completion([
                    "path": url.path,
                    "width": pixelWidth,
                    "height": pixelHeight,
                    "photoRect": [
                        "x": Int((frame.minX * scale).rounded()),
                        "y": Int(((pointSize.height - frame.maxY) * scale).rounded()),
                        "width": Int((frame.width * scale).rounded()),
                        "height": Int((frame.height * scale).rounded()),
                    ],
                ])
            } catch {
                completion(["error": error.localizedDescription])
            }
        }
    }

    private func writeNativeBenchmark(_ payload: [String: Any]) {
        guard let path = ProcessInfo.processInfo.environment[
            "LIGHTTABLE_NATIVE_BENCHMARK_OUTPUT"],
              JSONSerialization.isValidJSONObject(payload),
              let data = try? JSONSerialization.data(
                withJSONObject: payload, options: [.prettyPrinted, .sortedKeys])
        else { return }
        do {
            try data.write(to: URL(fileURLWithPath: path), options: .atomic)
            if ProcessInfo.processInfo.environment[
                "LIGHTTABLE_NATIVE_BENCHMARK_QUIT"] == "1" {
                DispatchQueue.main.async { NSApp.terminate(nil) }
            }
        } catch {
            sendEvent(["type": "error", "message": L("Could not save native benchmark.")])
        }
    }

    private func renameRoot(path: String, name: String) {
        let clean = normalized(path)
        let trimmed = name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, trimmed != ".", trimmed != "..",
              !trimmed.contains("/"), !trimmed.contains(":"), !trimmed.hasPrefix(".") else {
            sendEvent(["type": "error", "message": L("Enter a valid visible folder name.")])
            return
        }
        let source = URL(fileURLWithPath: clean)
        let destination = source.deletingLastPathComponent().appendingPathComponent(trimmed)
        guard !FileManager.default.fileExists(atPath: destination.path) else {
            sendEvent(["type": "error", "message": L("A folder with that name already exists.")])
            return
        }
        do {
            try FileManager.default.moveItem(at: source, to: destination)
            for index in sources.indices {
                if sources[index].path == clean {
                    sources[index].path = destination.path
                } else if sources[index].path.hasPrefix(clean + "/") {
                    sources[index].path = destination.path
                        + String(sources[index].path.dropFirst(clean.count))
                }
            }
            saveSources()
            migrateWebPreferences(from: clean, to: destination.path)
            launch(folder: destination.path)
        } catch {
            sendEvent(["type": "error", "message": L("Could not rename folder: {error}", ["error": error.localizedDescription])])
        }
    }

    private func migrateWebPreferences(from oldRoot: String, to newRoot: String) {
        let url = projectDir.appendingPathComponent("prefs.json")
        guard let data = try? Data(contentsOf: url),
              var prefs = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return }
        if var active = prefs["activeFolders"] as? [String: String],
           let relative = active.removeValue(forKey: oldRoot) {
            active[newRoot] = relative
            prefs["activeFolders"] = active
        }
        if let favorites = prefs["favoriteFolders"] as? [String] {
            prefs["favoriteFolders"] = favorites.map { path in
                path == oldRoot || path.hasPrefix(oldRoot + "/")
                    ? newRoot + String(path.dropFirst(oldRoot.count)) : path
            }
        }
        if let updated = try? JSONSerialization.data(
            withJSONObject: prefs, options: [.prettyPrinted, .sortedKeys]) {
            try? updated.write(to: url, options: .atomic)
        }
    }

    // MARK: Actions

    @objc func openFolder(_ sender: Any?) {
        if let picked = pickFolder(title: L("Add a folder to LightTable")) {
            addSource(picked)
            launch(folder: picked)
        }
    }

    @objc func addPhotos(_ sender: Any?) {
        let paths = pickPhotoSources()
        paths.forEach { addSource($0) }
        if let first = paths.first { launch(folder: first) }
    }

    @objc func importApplePhotos(_ sender: Any?) { presentPhotosPicker() }

    @objc func reloadUI(_ sender: Any?) { webView.reload() }

    @objc func restartServer(_ sender: Any?) {
        server.safeMode = false
        crashRestarts = 0
        launch(folder: resolvedLaunchFolder())
    }

    @objc func openInBrowser(_ sender: Any?) {
        NSWorkspace.shared.open(
            URL(string: "http://127.0.0.1:\(server.port)/")!)
    }

    @objc func showLog(_ sender: Any?) {
        NSWorkspace.shared.open(server.logURL)
    }

    @objc func revealExports(_ sender: Any?) {
        let dir = URL(fileURLWithPath: folder)
            .appendingPathComponent("film-exports")
        try? FileManager.default.createDirectory(
            at: dir, withIntermediateDirectories: true)
        NSWorkspace.shared.open(dir)
    }

    @objc func openHelp(_ sender: Any?) {
        sendEvent(["type": "menuCommand", "command": "help"])
    }

    @objc func performEditorCommand(_ sender: NSMenuItem) {
        guard let command = sender.representedObject as? String else { return }
        if command == "undo", menuBool("textEditing") {
            webView.undoManager?.undo()
            return
        }
        if command == "redo", menuBool("textEditing") {
            webView.undoManager?.redo()
            return
        }
        if command == "selectAll", menuBool("textEditing") {
            NSApp.sendAction(#selector(NSResponder.selectAll(_:)), to: nil, from: sender)
            return
        }
        sendEvent(["type": "menuCommand", "command": command])
    }

    // MARK: Menu

    @discardableResult
    private func addEditorItem(
        _ menu: NSMenu,
        title: String,
        command: String,
        key: String = "",
        modifiers: NSEvent.ModifierFlags = [.command],
        schemeShortcut: Bool = false
    ) -> NSMenuItem {
        let item = NSMenuItem(
            title: title,
            action: #selector(performEditorCommand(_:)),
            keyEquivalent: key)
        item.target = self
        item.representedObject = command
        if !key.isEmpty { item.keyEquivalentModifierMask = modifiers }
        editorCommandItems.append(item)
        if schemeShortcut { schemeCommandItems[command] = item }
        menu.addItem(item)
        return item
    }

    @discardableResult
    private func addSystemItem(
        _ menu: NSMenu,
        title: String,
        action: Selector,
        key: String = "",
        modifiers: NSEvent.ModifierFlags = [.command]
    ) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: key)
        if !key.isEmpty { item.keyEquivalentModifierMask = modifiers }
        menu.addItem(item)
        return item
    }

    private func addTopLevelMenu(_ menu: NSMenu, to main: NSMenu) {
        let item = NSMenuItem(title: menu.title, action: nil, keyEquivalent: "")
        item.submenu = menu
        main.addItem(item)
    }

    private func menuBool(_ key: String) -> Bool {
        if let value = editorMenuState[key] as? Bool { return value }
        return (editorMenuState[key] as? NSNumber)?.boolValue ?? false
    }

    private func menuInt(_ key: String) -> Int {
        (editorMenuState[key] as? NSNumber)?.intValue
            ?? editorMenuState[key] as? Int ?? 0
    }

    private func menuString(_ key: String) -> String {
        editorMenuState[key] as? String ?? ""
    }

    private func updateEditorMenuItems() {
        updateSchemeShortcuts()
        editorCommandItems.forEach { _ = validateMenuItem($0) }
    }

    private func updateSchemeShortcuts() {
        let classic = menuString("keyScheme") == "classic"
        let shortcuts: [String: (String, NSEvent.ModifierFlags)] = [
            "view:photo": ("g", []),
            "view:square": ("g", [.shift]),
            "view:detail": (classic ? "e" : "d", []),
            "survey": ("n", []),
            "compare": (classic ? "c" : "\\", []),
            "pane:crop": (classic ? "r" : "c", []),
            "pane:mask": ("m", []),
            "pane:heal": ("q", []),
        ]
        for (command, item) in schemeCommandItems {
            guard let shortcut = shortcuts[command] else { continue }
            item.keyEquivalent = shortcut.0
            item.keyEquivalentModifierMask = shortcut.1
        }
    }

    func validateMenuItem(_ menuItem: NSMenuItem) -> Bool {
        guard let command = menuItem.representedObject as? String else {
            return true
        }

        let ready = menuBool("ready")
        let textEditing = menuBool("textEditing")
        let hasPhoto = menuBool("hasPhoto")
        let editablePhoto = menuBool("editablePhoto")
        let selectedCount = menuInt("selectedCount")
        var checked = false

        switch command {
        case "view:photo": checked = menuString("viewMode") == "photo"
        case "view:square": checked = menuString("viewMode") == "square"
        case "view:detail": checked = menuString("viewMode") == "detail"
        case "pane:edit", "pane:film", "pane:mask", "pane:heal",
             "pane:crop", "pane:presets":
            let pane = String(command.dropFirst("pane:".count)) + "Pane"
            checked = menuString("activePane") == pane
        case "filmToggle": checked = menuBool("filmEnabled")
        case "compare": checked = menuBool("compareActive")
        case "softProof": checked = menuBool("softProofEnabled")
        case "toggleLibrary": checked = menuBool("libraryVisible")
        case "toggleFilmstrip": checked = menuBool("filmstripVisible")
        default:
            if command.hasPrefix("flag:") {
                checked = menuString("currentFlag")
                    == String(command.dropFirst("flag:".count))
            } else if command.hasPrefix("rating:") {
                checked = menuInt("currentRating")
                    == Int(command.dropFirst("rating:".count))
            } else if command.hasPrefix("label:") {
                checked = menuString("currentLabel")
                    == String(command.dropFirst("label:".count))
            }
        }
        menuItem.state = checked ? .on : .off

        if schemeCommandItems[command] != nil, textEditing { return false }
        switch command {
        case "undo":
            return textEditing ? (webView?.undoManager?.canUndo ?? false)
                : menuBool("canUndo")
        case "redo":
            return textEditing ? (webView?.undoManager?.canRedo ?? false)
                : menuBool("canRedo")
        case "selectAll":
            return textEditing || menuBool("hasImages")
        case "deselectAll":
            return !textEditing && menuBool("hasMultiSelection")
        case "preferences", "keyboardShortcuts", "search":
            return ready
        case "importCard", "importCatalog", "importSidecars",
             "backupCatalog", "findDuplicates":
            return ready && menuBool("catalogEnabled")
        case "newCollection", "newSmartCollection":
            return ready
        case "addToCollection":
            return menuBool("canAddToCollection")
        case "virtualCopy":
            return hasPhoto
        case "deleteVirtualCopy":
            return menuBool("currentVirtual")
        case "stack":
            return menuBool("canStack")
        case "unstack":
            return menuBool("canUnstack")
        case "matchExposure", "photoMerge":
            return selectedCount > 1
        case "buildPreviews", "batchAiMask":
            return selectedCount > 0
        case "enhancePhoto", "secondaryLoupe":
            return editablePhoto
        case "previousPhoto":
            return menuBool("hasPreviousPhoto")
        case "nextPhoto":
            return menuBool("hasNextPhoto")
        case "renamePhoto", "revealPhoto":
            return hasPhoto
        case "editExternal":
            return selectedCount > 0
        case "deleteRejected":
            return menuBool("hasRejected")
        case "copySettings":
            return editablePhoto
        case "pasteSettings":
            return menuBool("canPaste")
        case "pasteAllVisible":
            return menuBool("canPaste") && menuBool("hasImages")
        case "resetCrop":
            return editablePhoto && menuBool("hasCrop")
        case "resetMasks":
            return editablePhoto && menuBool("hasMasks")
        case "resetHealing":
            return editablePhoto && menuBool("hasHealing")
        case "resetLens":
            return editablePhoto && menuBool("hasLensCorrections")
        case "resetEdit", "resetFilm", "filmToggle", "pane:edit",
             "pane:film", "pane:mask", "pane:heal",
             "pane:crop", "pane:presets", "softProof", "zoomIn",
             "zoomOut", "zoomFit", "zoomActual":
            return editablePhoto
        case "compare":
            return menuBool("compareEnabled")
        case "savePreset":
            return editablePhoto
        case "exportPreset":
            return menuBool("hasSelectedPreset")
        case "importPreset":
            return ready
        case "exportPhotos":
            return menuBool("hasImages")
        case "survey":
            return selectedCount > 0 || menuBool("hasImages")
        case "toggleLibrary":
            return ready
        case "toggleFilmstrip":
            return ready && menuString("viewMode") == "detail"
        case "view:photo", "view:square", "view:detail":
            return ready
        default:
            if command.hasPrefix("flag:") || command.hasPrefix("rating:")
                || command.hasPrefix("label:") {
                return hasPhoto
            }
            return ready
        }
    }

    private func buildMenu() {
        let main = NSMenu()
        editorCommandItems.removeAll()
        schemeCommandItems.removeAll()

        let appMenu = NSMenu(title: "LightTable")
        appMenu.addItem(withTitle: L("About LightTable"),
                        action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)),
                        keyEquivalent: "")
#if canImport(Sparkle)
        let updateItem = appMenu.addItem(
            withTitle: L("Check for Updates…"),
            action: #selector(SPUStandardUpdaterController.checkForUpdates(_:)),
            keyEquivalent: "")
        updateItem.target = updaterController
#endif
        appMenu.addItem(.separator())
        addEditorItem(appMenu, title: L("Settings…"), command: "preferences",
                      key: ",")
        let servicesItem = NSMenuItem(
            title: L("Services"), action: nil, keyEquivalent: "")
        let servicesMenu = NSMenu(title: L("Services"))
        servicesItem.submenu = servicesMenu
        appMenu.addItem(servicesItem)
        NSApp.servicesMenu = servicesMenu
        appMenu.addItem(.separator())
        addSystemItem(appMenu, title: L("Hide LightTable"),
                      action: #selector(NSApplication.hide(_:)), key: "h")
        addSystemItem(appMenu, title: L("Hide Others"),
                      action: #selector(NSApplication.hideOtherApplications(_:)),
                      key: "h", modifiers: [.command, .option])
        addSystemItem(appMenu, title: L("Show All"),
                      action: #selector(NSApplication.unhideAllApplications(_:)))
        appMenu.addItem(.separator())
        addSystemItem(appMenu, title: L("Quit LightTable"),
                      action: #selector(NSApplication.terminate(_:)), key: "q")
        addTopLevelMenu(appMenu, to: main)

        let fileMenu = NSMenu(title: L("File"))
        let addPhotosItem = addSystemItem(
            fileMenu, title: L("Add Photos…"), action: #selector(addPhotos(_:)),
            key: "i", modifiers: [.command, .shift])
        addPhotosItem.target = self
        let importPhotosItem = addSystemItem(
            fileMenu, title: L("Import from Apple Photos…"),
            action: #selector(importApplePhotos(_:)))
        importPhotosItem.target = self
        let addFolderItem = addSystemItem(
            fileMenu, title: L("Add Folder…"), action: #selector(openFolder(_:)),
            key: "o")
        addFolderItem.target = self
        addEditorItem(fileMenu, title: L("Import from Card…"),
                      command: "importCard")
        fileMenu.addItem(.separator())
        addEditorItem(fileMenu, title: L("Export Photos…"),
                      command: "exportPhotos", key: "e",
                      modifiers: [.command, .shift])
        let revealExportsItem = addSystemItem(
            fileMenu, title: L("Reveal Export Folder in Finder"),
            action: #selector(revealExports(_:)))
        revealExportsItem.target = self
        fileMenu.addItem(.separator())
        addSystemItem(fileMenu, title: L("Close Window"),
                      action: #selector(NSWindow.performClose(_:)), key: "w")
        addTopLevelMenu(fileMenu, to: main)

        let editMenu = NSMenu(title: L("Edit"))
        addEditorItem(editMenu, title: L("Undo"), command: "undo", key: "z")
        addEditorItem(editMenu, title: L("Redo"), command: "redo", key: "z",
                      modifiers: [.command, .shift])
        editMenu.addItem(.separator())
        addSystemItem(editMenu, title: L("Cut"),
                      action: #selector(NSText.cut(_:)), key: "x")
        addSystemItem(editMenu, title: L("Copy"),
                      action: #selector(NSText.copy(_:)), key: "c")
        addSystemItem(editMenu, title: L("Paste"),
                      action: #selector(NSText.paste(_:)), key: "v")
        editMenu.addItem(.separator())
        addEditorItem(editMenu, title: L("Select All"), command: "selectAll",
                      key: "a")
        addEditorItem(editMenu, title: L("Deselect All"), command: "deselectAll",
                      key: "a", modifiers: [.command, .shift])
        editMenu.addItem(.separator())
        addEditorItem(editMenu, title: L("Search"), command: "search", key: "f")
        addTopLevelMenu(editMenu, to: main)

        let libraryMenu = NSMenu(title: L("Library"))
        addEditorItem(libraryMenu, title: L("New Collection…"),
                      command: "newCollection", key: "n")
        addEditorItem(libraryMenu, title: L("New Smart Collection…"),
                      command: "newSmartCollection", key: "n",
                      modifiers: [.command, .option])
        addEditorItem(libraryMenu, title: L("Add Selected to Collection"),
                      command: "addToCollection")
        libraryMenu.addItem(.separator())
        addEditorItem(libraryMenu, title: L("Create Virtual Copy…"),
                      command: "virtualCopy", key: "'")
        addEditorItem(libraryMenu, title: L("Delete Virtual Copy"),
                      command: "deleteVirtualCopy")
        addEditorItem(libraryMenu, title: L("Stack Selected Photos…"),
                      command: "stack")
        addEditorItem(libraryMenu, title: L("Unstack Photos"),
                      command: "unstack")
        libraryMenu.addItem(.separator())
        addEditorItem(libraryMenu, title: L("Match Total Exposure"),
                      command: "matchExposure")
        addEditorItem(libraryMenu, title: L("Build 1:1 Previews"),
                      command: "buildPreviews")
        addEditorItem(libraryMenu, title: L("Generate Subject Masks"),
                      command: "batchAiMask")
        libraryMenu.addItem(.separator())
        addEditorItem(libraryMenu, title: L("Import Catalog…"),
                      command: "importCatalog")
        addEditorItem(libraryMenu, title: L("Read XMP Sidecars…"),
                      command: "importSidecars")
        libraryMenu.addItem(.separator())
        addEditorItem(libraryMenu, title: L("Back Up Catalog Now"),
                      command: "backupCatalog")
        addEditorItem(libraryMenu, title: L("Find Duplicates"),
                      command: "findDuplicates")
        addTopLevelMenu(libraryMenu, to: main)

        let photoMenu = NSMenu(title: L("Photo"))
        addEditorItem(photoMenu, title: L("Previous Photo"),
                      command: "previousPhoto")
        addEditorItem(photoMenu, title: L("Next Photo"), command: "nextPhoto")
        photoMenu.addItem(.separator())
        addEditorItem(photoMenu, title: L("Flag as Pick"),
                      command: "flag:approved")
        addEditorItem(photoMenu, title: L("Reject"), command: "flag:skipped")
        addEditorItem(photoMenu, title: L("Unflag"), command: "flag:pending")

        let ratingItem = NSMenuItem(title: L("Set Rating"), action: nil,
                                    keyEquivalent: "")
        let ratingMenu = NSMenu(title: L("Set Rating"))
        for rating in 0...5 {
            addEditorItem(ratingMenu,
                          title: rating == 0 ? L("No Rating") : String(
                            repeating: "★", count: rating),
                          command: "rating:\(rating)", key: "\(rating)",
                          modifiers: [], schemeShortcut: true)
        }
        ratingItem.submenu = ratingMenu
        photoMenu.addItem(ratingItem)

        let labelItem = NSMenuItem(title: L("Set Colour Label"), action: nil,
                                   keyEquivalent: "")
        let labelMenu = NSMenu(title: L("Set Colour Label"))
        addEditorItem(labelMenu, title: L("None"), command: "label:none")
        for (label, title, key) in [
            ("red", L("Red"), "6"), ("yellow", L("Yellow"), "7"),
            ("green", L("Green"), "8"), ("blue", L("Blue"), "9"),
            ("purple", L("Purple"), ""),
        ] {
            addEditorItem(labelMenu, title: title, command: "label:\(label)",
                          key: key, modifiers: [], schemeShortcut: !key.isEmpty)
        }
        labelItem.submenu = labelMenu
        photoMenu.addItem(labelItem)
        photoMenu.addItem(.separator())
        addEditorItem(photoMenu, title: L("Rotate Left"),
                      command: "rotateLeft", key: "[")
        addEditorItem(photoMenu, title: L("Rotate Right"),
                      command: "rotateRight", key: "]")
        addEditorItem(photoMenu, title: L("Rename…"), command: "renamePhoto")
        addEditorItem(photoMenu, title: L("Show in Finder"),
                      command: "revealPhoto", key: "r")
        addEditorItem(photoMenu, title: L("Edit In…"),
                      command: "editExternal", key: "e")
        addEditorItem(photoMenu, title: L("Enhance Photo…"),
                      command: "enhancePhoto")
        addEditorItem(photoMenu, title: L("Photo Merge…"),
                      command: "photoMerge")
        photoMenu.addItem(.separator())
        addEditorItem(photoMenu, title: L("Move Rejected Photos to Trash…"),
                      command: "deleteRejected", key: "\u{8}")
        addTopLevelMenu(photoMenu, to: main)

        let developMenu = NSMenu(title: L("Develop"))
        addEditorItem(developMenu, title: L("Copy Edit Settings"),
                      command: "copySettings", key: "c",
                      modifiers: [.command, .shift])
        addEditorItem(developMenu, title: L("Paste Edit Settings"),
                      command: "pasteSettings", key: "v",
                      modifiers: [.command, .shift])
        addEditorItem(developMenu, title: L("Paste to All Visible Photos"),
                      command: "pasteAllVisible")
        developMenu.addItem(.separator())
        addEditorItem(developMenu, title: L("Reset Edit Adjustments"),
                      command: "resetEdit")
        addEditorItem(developMenu, title: L("Reset Film Settings"),
                      command: "resetFilm")
        addEditorItem(developMenu, title: L("Reset Crop & Geometry"), command: "resetCrop")
        addEditorItem(developMenu, title: L("Clear Masks"), command: "resetMasks")
        addEditorItem(developMenu, title: L("Clear Remove Corrections"),
                      command: "resetHealing")
        addEditorItem(developMenu, title: L("Reset Lens Corrections"),
                      command: "resetLens")
        developMenu.addItem(.separator())
        addEditorItem(developMenu, title: L("Light & Colour"),
                      command: "pane:edit")
        addEditorItem(developMenu, title: L("Film"), command: "pane:film")
        addEditorItem(developMenu, title: L("Masking"), command: "pane:mask",
                      schemeShortcut: true)
        addEditorItem(developMenu, title: L("Remove"), command: "pane:heal",
                      schemeShortcut: true)
        addEditorItem(developMenu, title: L("Crop"), command: "pane:crop",
                      schemeShortcut: true)
        developMenu.addItem(.separator())
        addEditorItem(developMenu, title: L("Enable Film Profile"),
                      command: "filmToggle")

        let presetItem = NSMenuItem(title: L("Presets"), action: nil,
                                    keyEquivalent: "")
        let presetMenu = NSMenu(title: L("Presets"))
        addEditorItem(presetMenu, title: L("Show Presets"),
                      command: "pane:presets")
        addEditorItem(presetMenu, title: L("Save Current Settings as Preset…"),
                      command: "savePreset")
        addEditorItem(presetMenu, title: L("Import Presets…"),
                      command: "importPreset")
        addEditorItem(presetMenu, title: L("Export Selected Preset…"),
                      command: "exportPreset")
        presetItem.submenu = presetMenu
        developMenu.addItem(presetItem)
        addTopLevelMenu(developMenu, to: main)

        let viewMenu = NSMenu(title: L("View"))
        addEditorItem(viewMenu, title: L("Photo Grid"), command: "view:photo",
                      schemeShortcut: true)
        addEditorItem(viewMenu, title: L("Square Grid"), command: "view:square",
                      schemeShortcut: true)
        addEditorItem(viewMenu, title: L("Detail"), command: "view:detail",
                      schemeShortcut: true)
        addEditorItem(viewMenu, title: L("Survey Selection"), command: "survey",
                      schemeShortcut: true)
        viewMenu.addItem(.separator())
        addEditorItem(viewMenu, title: L("Library Panel"),
                      command: "toggleLibrary")
        addEditorItem(viewMenu, title: L("Filmstrip"), command: "toggleFilmstrip")
        viewMenu.addItem(.separator())
        addEditorItem(viewMenu, title: L("Compare Before & After"),
                      command: "compare", schemeShortcut: true)
        addEditorItem(viewMenu, title: L("Soft Proof"), command: "softProof")
        viewMenu.addItem(.separator())
        addEditorItem(viewMenu, title: L("Zoom In"), command: "zoomIn", key: "=")
        addEditorItem(viewMenu, title: L("Zoom Out"), command: "zoomOut", key: "-")
        addEditorItem(viewMenu, title: L("Fit"), command: "zoomFit")
        addEditorItem(viewMenu, title: L("Actual Size"), command: "zoomActual")
        viewMenu.addItem(.separator())
        addSystemItem(viewMenu, title: L("Enter Full Screen"),
                      action: #selector(NSWindow.toggleFullScreen(_:)), key: "f",
                      modifiers: [.command, .control])
        addTopLevelMenu(viewMenu, to: main)

        let winMenu = NSMenu(title: L("Window"))
        addSystemItem(winMenu, title: L("Minimize"),
                      action: #selector(NSWindow.performMiniaturize(_:)), key: "m")
        addSystemItem(winMenu, title: L("Zoom"),
                      action: #selector(NSWindow.performZoom(_:)))
        addEditorItem(winMenu, title: L("Secondary Loupe"),
                      command: "secondaryLoupe")
        winMenu.addItem(.separator())
        addSystemItem(winMenu, title: L("Bring All to Front"),
                      action: #selector(NSApplication.arrangeInFront(_:)))
        addTopLevelMenu(winMenu, to: main)

        let helpMenu = NSMenu(title: L("Help"))
        let helpItem = addSystemItem(
            helpMenu, title: L("LightTable Help"), action: #selector(openHelp(_:)))
        helpItem.target = self
        addEditorItem(helpMenu, title: L("Keyboard Shortcuts"),
                      command: "keyboardShortcuts")
        helpMenu.addItem(.separator())
        let diagnosticsItem = NSMenuItem(
            title: L("Diagnostics"), action: nil, keyEquivalent: "")
        let diagnosticsMenu = NSMenu(title: L("Diagnostics"))
        let reloadItem = addSystemItem(
            diagnosticsMenu, title: L("Reload Interface"),
            action: #selector(reloadUI(_:)))
        reloadItem.target = self
        let restartItem = addSystemItem(
            diagnosticsMenu, title: L("Restart Rendering Service"),
            action: #selector(restartServer(_:)))
        restartItem.target = self
        let browserItem = addSystemItem(
            diagnosticsMenu, title: L("Open Interface in Browser"),
            action: #selector(openInBrowser(_:)))
        browserItem.target = self
        let logItem = addSystemItem(
            diagnosticsMenu, title: L("Show Server Log"),
            action: #selector(showLog(_:)))
        logItem.target = self
        diagnosticsMenu.addItem(.separator())
        let healthItem = addSystemItem(
            diagnosticsMenu, title: L("Library Health…"),
            action: #selector(openLibraryHealth(_:)))
        healthItem.target = self
        let safeModeItem = addSystemItem(
            diagnosticsMenu, title: L("Restart in Safe Mode"),
            action: #selector(restartInSafeMode(_:)))
        safeModeItem.target = self
        let recoveryItem = addSystemItem(
            diagnosticsMenu, title: L("Open Recovery Folder"),
            action: #selector(openRecoveryFolder(_:)))
        recoveryItem.target = self
        diagnosticsItem.submenu = diagnosticsMenu
        helpMenu.addItem(diagnosticsItem)
        addTopLevelMenu(helpMenu, to: main)

        NSApp.mainMenu = main
        NSApp.windowsMenu = winMenu
        NSApp.helpMenu = helpMenu
        updateSchemeShortcuts()
    }
}

// Show native hover help sooner, while respecting an explicit user preference.
UserDefaults.standard.register(defaults: ["NSInitialToolTipDelay": 700])

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
