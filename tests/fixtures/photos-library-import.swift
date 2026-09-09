// Compiled by test_photos_library_import.py with production Swift inserted.
// Fake PhotoKit only supplies assets/chunks; file IO, locking, counters, and
// cancellation use the production importer unchanged. No Photos access occurs.
import Foundation
import CryptoKit
import UniformTypeIdentifiers
import Darwin

enum PHAssetResourceType: Int { case photo, alternatePhoto, fullSizePhoto, pairedVideo }
enum PHAssetMediaType { case image }
typealias PHAssetResourceDataRequestID = Int
final class PHFetchOptions { var includeHiddenAssets = false; var includeAllBurstAssets = false }
final class PHAssetResource {
    let type: PHAssetResourceType
    let originalFilename: String
    let uniformTypeIdentifier = "public.jpeg"
    let data: Data
    let delay: Double
    let fail: Bool
    init(_ name: String, _ type: PHAssetResourceType = .photo, _ delay: Double = 0, _ fail: Bool = false) {
        self.originalFilename = name; self.type = type; self.delay = delay; self.fail = fail
        self.data = Data(("fixture-" + name).utf8)
    }
    static func assetResources(for asset: PHAsset) -> [PHAssetResource] { asset.resources }
}
final class PHAsset {
    static var fixtures: [PHAsset] = []
    let localIdentifier: String
    let resources: [PHAssetResource]
    init(_ id: String, _ resources: [PHAssetResource]) { localIdentifier = id; self.resources = resources }
    static func fetchAssets(withLocalIdentifiers ids: [String], options: PHFetchOptions) -> AssetResult {
        AssetResult(fixtures.filter { ids.contains($0.localIdentifier) })
    }
    static func fetchAssets(with type: PHAssetMediaType, options: PHFetchOptions) -> AssetResult { AssetResult(fixtures) }
}
final class AssetResult {
    let values: [PHAsset]
    var count: Int { values.count }
    init(_ values: [PHAsset]) { self.values = values }
    func enumerateObjects(_ body: (PHAsset, Int, UnsafeMutablePointer<ObjCBool>) -> Void) {
        var stop = ObjCBool(false)
        for (index, asset) in values.enumerated() { body(asset, index, &stop); if stop.boolValue { break } }
    }
}
final class PHAssetResourceRequestOptions {
    var isNetworkAccessAllowed = false
    var progressHandler: ((Double) -> Void)?
}
final class PHAssetResourceManager {
    static let shared = PHAssetResourceManager()
    static func `default`() -> PHAssetResourceManager { shared }
    private let lock = NSLock()
    private var nextID = 0
    private var cancelled = Set<Int>()
    var requests = 0
    var onRequest: ((PHAssetResource) -> Void)?
    func requestData(for resource: PHAssetResource, options: PHAssetResourceRequestOptions?, dataReceivedHandler: @escaping (Data) -> Void, completionHandler: @escaping (Error?) -> Void) -> Int {
        lock.lock(); nextID += 1; let id = nextID; requests += 1; lock.unlock()
        onRequest?(resource)
        DispatchQueue.global().async {
            options?.progressHandler?(0.5)
            if resource.delay > 0 { Thread.sleep(forTimeInterval: resource.delay) }
            self.lock.lock(); let cancelled = self.cancelled.contains(id); self.lock.unlock()
            if cancelled { completionHandler(CocoaError(.userCancelled)); return }
            let split = resource.data.count / 2
            dataReceivedHandler(resource.data.prefix(split))
            dataReceivedHandler(resource.data.suffix(from: split))
            completionHandler(resource.fail ? CocoaError(.fileReadUnknown) : nil)
        }
        return id
    }
    func cancelDataRequest(_ id: Int) { lock.lock(); cancelled.insert(id); lock.unlock() }
}

IMPORTER_SOURCE

struct FolderSource: Codable, Equatable { var path: String; var favorite: Bool }
let defaultPhotoFolder = "/fixture/Pictures"

func require(_ value: @autoclosure () -> Bool, _ message: String) {
    if !value() { fatalError(message) }
}
func wait(_ body: () -> Bool) {
    let deadline = Date().addingTimeInterval(6)
    while !body(), Date() < deadline { RunLoop.main.run(until: Date().addingTimeInterval(0.01)) }
    require(body(), "fixture timed out")
}

private func testImporter() throws {
let root = FileManager.default.temporaryDirectory.appendingPathComponent("lighttable-photos-fixture-" + UUID().uuidString)
try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
defer { try? FileManager.default.removeItem(at: root) }
func copied(_ folder: URL) -> [URL] {
    (FileManager.default.enumerator(at: folder, includingPropertiesForKeys: nil)?.allObjects as? [URL] ?? []).filter { $0.pathExtension == "jpg" || $0.pathExtension == "dng" }
}
func partials(_ folder: URL) -> [URL] {
    (FileManager.default.enumerator(at: folder, includingPropertiesForKeys: nil)?.allObjects as? [URL] ?? []).filter { $0.pathExtension == "partial" }
}
func run(_ assets: [PHAsset], folder: URL, cancelAtResource: String? = nil, selection: [String]? = nil) -> [String: Any] {
    PHAsset.fixtures = assets
    var terminal: [String: Any]?
    let importer = PhotosLibraryImporter(directory: folder, assetIdentifiers: selection) { event in
        if event["state"] as? String != "running" { terminal = event }
    }
    if let cancelAtResource {
        PHAssetResourceManager.shared.onRequest = { resource in
            if resource.originalFilename == cancelAtResource {
                DispatchQueue.main.async { importer.cancel() }
            }
        }
    }
    defer { PHAssetResourceManager.shared.onRequest = nil }
    importer.start()
    wait { terminal != nil }
    require(partials(folder).isEmpty, "partial files survived")
    return terminal!
}
let pair = PHAsset("same-asset", [PHAssetResource("same.jpg"), PHAssetResource("same.dng", .alternatePhoto), PHAssetResource("edited.jpg", .fullSizePhoto), PHAssetResource("video.mov", .pairedVideo)])
var result = run([pair], folder: root)
require(result["imported"] as? Int == 2 && result["total"] as? Int == 2, "original pair counts")
require(copied(root).count == 2, "adjusted/video excluded")
let original = copied(root).first { $0.pathExtension == "jpg" }!
let originalData = try Data(contentsOf: original)
require(originalData == Data("fixture-same.jpg".utf8), "streaming preserves bytes")
let before = PHAssetResourceManager.shared.requests
let unicode = PHAsset("new-asset", [PHAssetResource(String(repeating: "👨‍👩‍👦", count: 80) + ".jpg")])
result = run([pair, unicode], folder: root)
require(result["imported"] as? Int == 1 && result["existing"] as? Int == 2, "rerun must add only new")
require(PHAssetResourceManager.shared.requests - before == 1, "dedup must skip downloading")
require(copied(root).count == 3, "long Unicode filenames")
let chosen = PHAsset("chosen", [PHAssetResource("chosen.jpg"), PHAssetResource("chosen.dng", .alternatePhoto)])
let unchosen = PHAsset("unchosen", [PHAssetResource("unchosen.jpg")])
let selectionBefore = PHAssetResourceManager.shared.requests
result = run([chosen, unchosen], folder: root, selection: ["chosen", "chosen", "deleted"])
require(result["imported"] as? Int == 2 && result["failures"] as? Int == 1, "selected originals and missing asset counts")
require(PHAssetResourceManager.shared.requests - selectionBefore == 2, "unselected asset must never download")
require(!copied(root).contains { $0.lastPathComponent.hasPrefix("unchosen") }, "unselected original was copied")
result = run([chosen, unchosen], folder: root, selection: ["chosen"])
require(result["existing"] as? Int == 2 && result["imported"] as? Int == 0, "selected retries share deterministic paths")
result = run([chosen, unchosen], folder: root, selection: [])
require(result["total"] as? Int == 0, "empty selection must never import the whole library")
let failed = PHAsset("failed-asset", [PHAssetResource("fail.jpg", .photo, 0, true)])
result = run([failed], folder: root)
require(result["failures"] as? Int == 1 && result["imported"] as? Int == 0, "failed transfer counted")
let fast = PHAsset("fast-asset", [PHAssetResource("fast.jpg")])
let slow = PHAsset("slow-asset", [PHAssetResource("slow.jpg", .photo, 0.5)])
result = run([fast, slow], folder: root, cancelAtResource: "slow.jpg")
require(result["state"] as? String == "cancelled" && result["imported"] as? Int == 1, "cancel keeps completed copies")
result = run([fast, slow], folder: root)
require(result["imported"] as? Int == 1 && result["existing"] as? Int == 1, "cancelled run resumes without duplicates")
result = run([], folder: root)
require(result["total"] as? Int == 0 && result["imported"] as? Int == 0, "empty library counts")
PHAsset.fixtures = [PHAsset("lock-asset", [PHAssetResource("lock.jpg", .photo, 1)])]
var firstResult: [String: Any]?
var lockResult: [String: Any]?
let first = PhotosLibraryImporter(directory: root) { event in if event["state"] as? String != "running" { firstResult = event } }
PHAssetResourceManager.shared.onRequest = { _ in
    DispatchQueue.main.async {
        PHAssetResourceManager.shared.onRequest = nil
        let second = PhotosLibraryImporter(directory: root) { event in if event["state"] as? String != "running" { lockResult = event } }
        second.start()
    }
}
first.start()
wait { lockResult != nil }
require(lockResult?["state"] as? String == "error", "concurrent import locked out")
first.cancel()
wait { firstResult != nil }
require(partials(root).isEmpty, "cancellation removed partial")
print("PASS: byte-preserving stream; original-only RAW+JPEG; dedup/new assets; Unicode filename; transfer failure; cancellation/resume; empty library; concurrent import lock")

}

private final class FirstRunHarness {
    let defaults: UserDefaults
    let environment: [String: String]
    let workspace: URL
    var firstRun = false
    var folder = ""
    var sources: [FolderSource] = []
    var pendingSetupFolderEvent: [String: Any]?
    var pendingSetupCatalogEvent: [String: Any]?
    var launches: [String] = []
    var photosLibraryImportEvent: [String: Any]?
    var events: [[String: Any]] = []
    init(defaults: UserDefaults, environment: [String: String] = [:], workspace: URL) {
        self.defaults = defaults
        self.environment = environment
        self.workspace = workspace
    }
    func begin() {
        INITIALIZATION_SOURCE
        let clean = folder
        STARTUP_PERSISTENCE_SOURCE
    }
    func complete() { finishFirstRun() }
    func replay() { replaySetupEvents() }
    func addFolder(_ path: String) { addSource(path); folder = path }
    func importCatalog(_ body: [String: Any]) { completeSetupCatalogImport(body) }
    private func launch(folder: String) {
        self.folder = folder
        launches.append(folder)
        let clean = folder
        STARTUP_PERSISTENCE_SOURCE
    }
    private func gettingStartedFolder() throws -> URL { workspace }
    private func showFatal(_ message: String) { fatalError(message) }
    private func sendEvent(_ payload: [String: Any]) { events.append(payload) }
    FIRST_RUN_METHODS
}

private func testFirstRun() throws {
    let suite = "org.lighttable.test.first-run." + UUID().uuidString
    let defaults = UserDefaults(suiteName: suite)!
    defer { defaults.removePersistentDomain(forName: suite) }
    let root = FileManager.default.temporaryDirectory.appendingPathComponent(suite)
    try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: root) }
    let workspace = root.appendingPathComponent("Getting Started")
    try FileManager.default.createDirectory(at: workspace, withIntermediateDirectories: true)
    func reset() { defaults.removePersistentDomain(forName: suite) }
    func shell(_ environment: [String: String] = [:]) -> FirstRunHarness {
        FirstRunHarness(defaults: defaults, environment: environment, workspace: workspace)
    }
    let fresh = shell()
    fresh.begin()
    require(fresh.firstRun && fresh.folder == workspace.path && fresh.sources.isEmpty,
            "fresh install must open an empty workspace")
    require(defaults.object(forKey: "photoFolder") == nil && defaults.object(forKey: "folderSources") == nil,
            "automatic startup must not acknowledge setup or save Pictures")
    let reload = shell()
    reload.begin()
    require(reload.firstRun, "uncompleted first run must survive restart")
    fresh.pendingSetupFolderEvent = ["type": "setupFolderSelected", "path": root.path]
    fresh.photosLibraryImportEvent = ["type": "photosLibraryImport", "state": "completed", "imported": 3]
    fresh.replay()
    fresh.replay()
    require(fresh.events.count == 4, "terminal setup events must survive multiple page navigations")
    fresh.complete()
    require(defaults.integer(forKey: "firstRunCompleted") == 1 && defaults.string(forKey: "photoFolder") == workspace.path,
            "skip must persist completion and the empty workspace")
    require(fresh.pendingSetupFolderEvent == nil && fresh.photosLibraryImportEvent == nil,
            "acknowledgement must clear terminal events")
    let event = fresh.events.last!
    require(event["firstRun"] as? Bool == false && event["photosLibraryImportAvailable"] as? Bool == true,
            "completion must publish current setup state and capability")
    let skipped = shell()
    skipped.begin()
    require(!skipped.firstRun && skipped.folder == workspace.path,
            "restart after skip must never fall back to Pictures")
    reset()
    let whitespace = shell(["LIGHTTABLE_DIR": " \n "])
    whitespace.begin()
    require(whitespace.firstRun, "empty environment override is not an existing library")
    reset()
    let automated = shell(["LIGHTTABLE_DIR": "  /fixture/automation  "])
    automated.begin()
    require(!automated.firstRun && automated.folder == "/fixture/automation",
            "explicit environment path must retain automated startup behavior")
    require(defaults.object(forKey: "photoFolder") == nil && defaults.object(forKey: "folderSources") == nil,
            "automated launch must not save native user preferences")
    reset()
    defaults.set("/fixture/existing", forKey: "photoFolder")
    let existing = shell()
    existing.begin()
    require(!existing.firstRun && existing.folder == "/fixture/existing", "existing folder preference skips setup")
    reset()
    defaults.set(try JSONEncoder().encode([FolderSource(path: "/fixture/source", favorite: true)]), forKey: "folderSources")
    let savedSources = shell()
    savedSources.begin()
    require(!savedSources.firstRun && savedSources.folder == "/fixture/source", "existing sources skip setup")
    reset()
    let selected = shell()
    selected.begin()
    selected.addFolder(root.path)
    selected.photosLibraryImportEvent = ["type": "photosLibraryImport", "state": "running"]
    selected.complete()
    require(selected.photosLibraryImportEvent?["state"] as? String == "running", "completion must retain an active background import")
    let resumed = shell()
    resumed.begin()
    require(!resumed.firstRun && resumed.folder == root.path && resumed.sources.contains { $0.path == root.path },
            "successful folder choice must persist across restart")
    reset()
    let catalog = shell()
    catalog.begin()
    let firstFolder = root.appendingPathComponent("First photos")
    let secondFolder = root.appendingPathComponent("Second photos")
    try FileManager.default.createDirectory(at: firstFolder, withIntermediateDirectories: true)
    try FileManager.default.createDirectory(at: secondFolder, withIntermediateDirectories: true)
    let plainFile = root.appendingPathComponent("not-a-folder.jpg")
    try Data("fixture".utf8).write(to: plainFile)
    catalog.importCatalog([
        "paths": [root.appendingPathComponent("missing").path, plainFile.path,
                  "relative-path", firstFolder.path, firstFolder.path + "/.", secondFolder.path],
        "matched": 3, "unmatched": 2,
    ])
    require(catalog.launches == [firstFolder.path] && catalog.sources.count == 2,
            "catalog handoff must register only available distinct directories and launch first")
    require(defaults.string(forKey: "photoFolder") == firstFolder.path,
            "catalog handoff must persist native startup beyond Getting Started")
    catalog.replay()
    catalog.replay()
    require(catalog.events.count == 2 && catalog.events.last?["matched"] as? Int == 3
            && catalog.events.last?["unmatched"] as? Int == 2,
            "catalog counts must survive server and page reloads")
    catalog.complete()
    require(catalog.pendingSetupCatalogEvent == nil, "completion must clear catalog handoff")
    let importedRestart = shell()
    importedRestart.begin()
    require(importedRestart.folder == firstFolder.path && importedRestart.sources.count == 2,
            "catalog sources and chosen startup must survive native restart")
    reset()
    let offlineCatalog = shell()
    offlineCatalog.begin()
    offlineCatalog.importCatalog(["paths": [root.appendingPathComponent("offline").path],
                                 "matched": 0, "unmatched": 4])
    require(offlineCatalog.launches.isEmpty && offlineCatalog.sources.isEmpty,
            "offline catalog handoff must not launch or register missing sources")
    require(offlineCatalog.events.last?["type"] as? String == "setupCatalogImported"
            && offlineCatalog.events.last?["unmatched"] as? Int == 4,
            "offline catalog still reports its terminal counts")
    print("PASS: fresh/existing/environment startup, no implicit indexing, skip/folder/catalog persistence, validated source handoff, event replay and acknowledgement")
}
if CommandLine.arguments.last == "first-run" { try testFirstRun() }
else { try testImporter() }
