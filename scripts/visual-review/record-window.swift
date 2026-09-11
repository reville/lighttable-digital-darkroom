// SPDX-License-Identifier: GPL-3.0-only
import Foundation
import AppKit
import ScreenCaptureKit
import AVFoundation
import CoreGraphics
import QuartzCore

// Dedicated PID/window capture; never captures a display or activates an application.
final class Recorder: NSObject, SCStreamOutput, SCStreamDelegate {
    let writer: AVAssetWriter
    let input: AVAssetWriterInput
    let ready: URL
    let metadata: URL
    var firstPTS: CMTime?
    var firstEpochMs = 0.0
    var lastPTS = 0.0
    var frames = 0
    var dropped = 0
    var failure: String?
    var sampleStatuses: [String: Int] = [:]
    init(output: URL, ready: URL, metadata: URL, width: Int, height: Int) throws {
        self.ready = ready; self.metadata = metadata
        writer = try AVAssetWriter(outputURL: output, fileType: .mov)
        input = AVAssetWriterInput(mediaType: .video, outputSettings: [
            AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: width,
            AVVideoHeightKey: height, AVVideoCompressionPropertiesKey: [
                AVVideoAverageBitRateKey: 16_000_000, AVVideoMaxKeyFrameIntervalKey: 60]])
        input.expectsMediaDataInRealTime = true
        super.init()
        writer.add(input)
        guard writer.startWriting() else { throw writer.error! }
    }
    func stream(_ stream: SCStream, didStopWithError error: Error) { failure = error.localizedDescription }
    func stream(_ stream: SCStream, didOutputSampleBuffer sample: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .screen else { return }
        let attachments = CMSampleBufferGetSampleAttachmentsArray(sample, createIfNecessary: false) as? [[SCStreamFrameInfo: Any]]
        let status = attachments?.first?[.status] as? Int ?? -1
        sampleStatuses[String(status), default: 0] += 1
        guard sample.isValid, status == SCFrameStatus.complete.rawValue else { return }
        let pts = CMSampleBufferGetPresentationTimeStamp(sample)
        guard input.isReadyForMoreMediaData else { dropped += 1; return }
        if firstPTS == nil {
            firstPTS = pts
            firstEpochMs = (Date().timeIntervalSince1970 + pts.seconds - CACurrentMediaTime()) * 1000
            writer.startSession(atSourceTime: pts)
        }
        if input.append(sample) {
            frames += 1; lastPTS = pts.seconds
            if frames == 1 { try? Data("ready".utf8).write(to: ready, options: .atomic) }
        } else { failure = writer.error?.localizedDescription ?? "Video append failed" }
    }
    func finish(window: SCWindow) async throws {
        let stopPTS = CMClockGetTime(CMClockGetHostTimeClock())
        if firstPTS != nil {
            writer.endSession(atSourceTime: stopPTS)
            input.markAsFinished()
            await writer.finishWriting()
        } else {
            writer.cancelWriting()
            failure = failure ?? "No complete screen frames were delivered; the session may be locked or the window unavailable"
        }
        let result: [String: Any] = ["windowId": window.windowID,
            "pid": window.owningApplication?.processID ?? 0, "sampleStatuses": sampleStatuses,
            "firstFrameEpochMs": firstEpochMs, "frames": frames, "droppedFrames": dropped,
            "durationSeconds": firstPTS.map { stopPTS.seconds - $0.seconds } ?? 0,
            "lastCompleteFrameSeconds": firstPTS.map { lastPTS - $0.seconds } ?? 0,
            "width": window.frame.width, "height": window.frame.height,
            "writerStatus": writer.status.rawValue,
            "error": failure ?? writer.error?.localizedDescription ?? ""]
        try JSONSerialization.data(withJSONObject: result, options: [.prettyPrinted, .sortedKeys]).write(to: metadata)
        guard writer.status == .completed, frames > 0, failure == nil else {
            throw NSError(domain: "Recording failed", code: 1)
        }
    }
}

@main struct Main {
    static func main() async {
        do {
            let args = CommandLine.arguments
            // Initialize WindowServer without a Dock icon, window or activation.
            _ = await MainActor.run { NSApplication.shared.setActivationPolicy(.prohibited) }
            if args.count == 2 && args[1] == "--preflight" {
                let allowed = CGPreflightScreenCaptureAccess()
                let session = CGSessionCopyCurrentDictionary() as? [String: Any]
                let locked = session?["CGSSessionScreenIsLocked"] as? Bool ?? false
                let value: [String: Any] = ["screenRecordingAllowed": allowed, "sessionLocked": locked]
                print(String(data: try JSONSerialization.data(withJSONObject: value, options: .sortedKeys), encoding: .utf8)!)
                exit(allowed && !locked ? 0 : 2)
            }
            guard args.count == 7, let pid = Int32(args[1]), let duration = Double(args[6]),
                  duration >= 1, duration <= 600 else {
                throw NSError(domain: "Usage: record-window PID output.mov ready-file stop-file metadata.json seconds", code: 2)
            }
            guard CGPreflightScreenCaptureAccess() else {
                throw NSError(domain: "Screen Recording permission is required; no permission prompt was opened", code: 3)
            }
            let deadline = Date().addingTimeInterval(30)
            var selected: SCWindow?
            while Date() < deadline {
                let content = try await SCShareableContent.excludingDesktopWindows(true, onScreenWindowsOnly: true)
                let windows = content.windows.filter { $0.owningApplication?.processID == pid && $0.frame.width >= 1000 && $0.frame.height >= 600 }
                if windows.count == 1 { selected = windows[0]; break }
                if windows.count > 1 { throw NSError(domain: "Ambiguous target windows", code: 4) }
                try await Task.sleep(nanoseconds: 100_000_000)
            }
            guard let window = selected else { throw NSError(domain: "No unique visible window for the launched PID", code: 5) }
            let config = SCStreamConfiguration()
            config.width = Int(window.frame.width) / 2 * 2
            config.height = Int(window.frame.height) / 2 * 2
            config.minimumFrameInterval = CMTime(value: 1, timescale: 60)
            config.queueDepth = 6; config.showsCursor = false
            let recorder = try Recorder(output: URL(fileURLWithPath: args[2]), ready: URL(fileURLWithPath: args[3]),
                metadata: URL(fileURLWithPath: args[5]), width: config.width, height: config.height)
            let stream = SCStream(filter: SCContentFilter(desktopIndependentWindow: window), configuration: config, delegate: recorder)
            let queue = DispatchQueue(label: "lighttable.review.capture")
            try stream.addStreamOutput(recorder, type: .screen, sampleHandlerQueue: queue)
            try await stream.startCapture()
            let end = Date().addingTimeInterval(duration)
            while Date() < end && !FileManager.default.fileExists(atPath: args[4]) {
                try await Task.sleep(nanoseconds: 100_000_000)
            }
            try await stream.stopCapture()
            queue.sync {}
            try await recorder.finish(window: window)
        } catch { fputs("\(error)\n", stderr); exit(1) }
    }
}
