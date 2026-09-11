// SPDX-License-Identifier: GPL-3.0-only
import Foundation
import UniformTypeIdentifiers

// Providers may finish synchronously, fail, or deliver a temporary URL that
// stops existing as soon as their callback returns. No Photos access is used.
final class NSItemProvider {
    let suggestedName: String?
    let registeredTypeIdentifiers: [String]
    let payload: Data
    let delay: TimeInterval
    let fails: Bool
    var requested = false
    init(_ name: String, type: String = "public.jpeg", delay: TimeInterval = 0, fails: Bool = false) {
        suggestedName = name; registeredTypeIdentifiers = [type]
        payload = Data(("photo:" + name).utf8); self.delay = delay; self.fails = fails
    }
    func loadFileRepresentation(forTypeIdentifier type: String, completionHandler: @escaping (URL?, Error?) -> Void) -> Progress {
        requested = true
        let progress = Progress(totalUnitCount: 1)
        progress.cancellationHandler = { completionHandler(nil, CocoaError(.userCancelled)) }
        func deliver() {
            if fails { completionHandler(nil, CocoaError(.fileReadUnknown)); return }
            let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".jpg")
            try! payload.write(to: url)
            completionHandler(url, nil)
            try! FileManager.default.removeItem(at: url)
        }
        if delay == 0 { deliver() }
        else { DispatchQueue.global().asyncAfter(deadline: .now() + delay) { deliver() } }
        return progress
    }
}

IMPORTER_SOURCE

func require(_ value: @autoclosure () -> Bool, _ message: String) {
    if !value() { fatalError(message) }
}
func wait(_ predicate: () -> Bool) {
    let end = Date().addingTimeInterval(5)
    while !predicate(), Date() < end { RunLoop.main.run(until: Date().addingTimeInterval(0.01)) }
    require(predicate(), "Import timed out")
}
let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
defer { try? FileManager.default.removeItem(at: root) }
let existing = root.appendingPathComponent("IMG.jpeg")
let untouched = Data("existing original".utf8)
try untouched.write(to: existing)
var terminal: [String: Any]?
var updates = 0
let providers = [NSItemProvider("IMG.HEIC"), NSItemProvider("IMG.HEIC"), NSItemProvider(".hidden.jpg"),
                 NSItemProvider("failed.jpg", fails: true), NSItemProvider("video.mov", type: "public.movie")]
private let importer = SelectedPhotosImporter(providers: providers, directory: root) { event in
    if event["state"] as? String == "running" { updates += 1 }
    else { terminal = event }
}
importer.start()
wait { terminal != nil }
require(terminal?["imported"] as? Int == 3, "Missing successful copies: \(terminal!) files: \(try! FileManager.default.contentsOfDirectory(atPath: root.path))")
require(terminal?["failures"] as? Int == 2, "Failure/unsupported count lost")
require(updates == 5, "Progress not emitted per selection")
require(try! Data(contentsOf: existing) == untouched, "Existing photo overwritten")
require(try! Data(contentsOf: root.appendingPathComponent("IMG-2.jpeg")) == providers[0].payload, "Ephemeral file lost or incompatible suffix")
require(try! Data(contentsOf: root.appendingPathComponent("IMG-3.jpeg")) == providers[1].payload, "Repeated name lost")
require(FileManager.default.fileExists(atPath: root.appendingPathComponent("hidden.jpeg").path), "Hidden imported photo")
require(!providers[4].requested, "Video transferred")
require(!(try! FileManager.default.contentsOfDirectory(atPath: root.path)).contains { $0.hasSuffix(".partial") }, "Staging files survived")

terminal = nil
private var cancelled: SelectedPhotosImporter!
let slow = [NSItemProvider("slow.jpg", delay: 0.2), NSItemProvider("never.jpg")]
cancelled = SelectedPhotosImporter(providers: slow, directory: root) { event in
    if event["state"] as? String == "running" { cancelled.cancel() }
    else { terminal = event }
}
cancelled.start()
wait { terminal != nil }
require(terminal?["state"] as? String == "cancelled", "Cancel did not finish")
RunLoop.main.run(until: Date().addingTimeInterval(0.4))
require(!FileManager.default.fileExists(atPath: root.appendingPathComponent("slow.jpeg").path), "Late callback copied after cancellation")
require(!slow[1].requested, "Started another transfer after stop")

terminal = nil
let missing = root.appendingPathComponent("missing/destination")
private let failed = SelectedPhotosImporter(providers: [NSItemProvider("unwritable.jpg")], directory: missing) { event in
    if event["state"] as? String != "running" { terminal = event }
}
failed.start()
wait { terminal != nil }
require(terminal?["failures"] as? Int == 1, "Destination failure was hidden")
print("PASS: ephemeral files, progress, compatible extension, name collisions, failures, stop, and late callbacks")
