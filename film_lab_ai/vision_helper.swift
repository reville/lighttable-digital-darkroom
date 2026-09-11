// SPDX-License-Identifier: GPL-3.0-only
import Foundation
import Vision
import CoreImage
import CoreGraphics
import AVFoundation
import ImageIO

private enum HelperFailure: LocalizedError {
    case message(String)

    var errorDescription: String? {
        switch self { case .message(let value): return value }
    }
}

private func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(1)
}

private func writeJSON(_ payload: [String: Any]) throws {
    let data = try JSONSerialization.data(withJSONObject: payload, options: [])
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data("\n".utf8))
}

private func checkedURL(_ path: String, kind: String) throws -> URL {
    let url = URL(fileURLWithPath: path)
    guard FileManager.default.fileExists(atPath: url.path) else {
        throw HelperFailure.message("\(kind) not found")
    }
    return url
}

/* Culling cues.
 *
 * Everything below reports in image-normalized coordinates with the origin at
 * the top left, because the Python scorer indexes preview pixels that way.
 * Vision's own space is bottom-up, so every y is flipped exactly once, here.
 */

private func topDownBox(_ box: CGRect) -> [String: Double] {
    return ["x": Double(box.origin.x), "y": Double(1 - box.origin.y - box.size.height),
            "width": Double(box.size.width), "height": Double(box.size.height)]
}

private func eyeContour(_ region: VNFaceLandmarkRegion2D?,
                        face: CGRect) -> [[Double]] {
    guard let region, region.pointCount >= 4 else { return [] }
    return region.normalizedPoints.map { point in
        let x = face.origin.x + CGFloat(point.x) * face.size.width
        let y = face.origin.y + CGFloat(point.y) * face.size.height
        return [Double(x), Double(1 - y)]
    }
}

/// A coarse subject mask is enough to answer "is the subject the sharp part",
/// and small enough to carry back as JSON instead of a file per photo.
@available(macOS 14.0, *)
private func coarseSubjectMask(_ handler: VNImageRequestHandler,
                               edge: Int) -> [String: Any]? {
    let request = VNGenerateForegroundInstanceMaskRequest()
    guard (try? handler.perform([request])) != nil,
          let observation = request.results?.first else { return nil }
    guard let buffer = try? observation.generateScaledMaskForImage(
        forInstances: observation.allInstances, from: handler) else { return nil }
    let source = CIImage(cvPixelBuffer: buffer)
    let renderer = CIContext(options: [.useSoftwareRenderer: false])
    guard let full = renderer.createCGImage(source, from: source.extent),
          let context = CGContext(
            data: nil, width: edge, height: edge, bitsPerComponent: 8,
            bytesPerRow: edge, space: CGColorSpaceCreateDeviceGray(),
            bitmapInfo: CGImageAlphaInfo.none.rawValue) else { return nil }
    context.interpolationQuality = .medium
    context.draw(full, in: CGRect(x: 0, y: 0, width: edge, height: edge))
    guard let data = context.data else { return nil }
    let bytes = Data(bytes: data, count: edge * edge)
    var covered = 0
    for value in bytes where value >= 128 { covered += 1 }
    return ["edge": edge,
            "coverage": Double(covered) / Double(edge * edge),
            "mask": bytes.base64EncodedString()]
}

private func analyzeImage(_ imageURL: URL) throws -> [String: Any] {
    let classify = VNClassifyImageRequest()
    let recognizeText = VNRecognizeTextRequest()
    recognizeText.recognitionLevel = .accurate
    recognizeText.usesLanguageCorrection = true
    // Landmarks carry the face rectangle too, so culling gains eye contours
    // without a second detection pass over the image.
    let detectFaces = VNDetectFaceLandmarksRequest()
    let handler = VNImageRequestHandler(url: imageURL, options: [:])
    try handler.perform([classify, recognizeText, detectFaces])

    var tags: [String] = []
    var seenTags = Set<String>()
    for observation in (classify.results ?? []).prefix(40) {
        guard observation.confidence >= 0.08 else { continue }
        let label = observation.identifier
            .replacingOccurrences(of: "_", with: " ")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        let key = label.lowercased()
        if !label.isEmpty && !seenTags.contains(key) {
            seenTags.insert(key)
            tags.append(label)
        }
        if tags.count == 14 { break }
    }

    var text: [String] = []
    var seenText = Set<String>()
    var textCoverage = 0.0
    var textLines = 0
    for observation in recognizeText.results ?? [] {
        guard let candidate = observation.topCandidates(1).first,
              candidate.confidence >= 0.45 else { continue }
        let value = candidate.string
            .trimmingCharacters(in: .whitespacesAndNewlines)
        let key = value.lowercased()
        guard value.rangeOfCharacter(from: .alphanumerics) != nil else { continue }
        // Coverage counts every recognized line, including repeats, because
        // it measures how much of the frame is print.
        textLines += 1
        textCoverage += Double(observation.boundingBox.width
                               * observation.boundingBox.height)
        if !seenText.contains(key) {
            seenText.insert(key)
            text.append(value)
        }
    }

    let faceObservations = detectFaces.results ?? []
    let faces: [[String: Any]] = faceObservations.map { observation in
        let box = observation.boundingBox
        return [
            "x": box.origin.x, "y": box.origin.y,
            "width": box.size.width, "height": box.size.height,
            "confidence": observation.confidence,
        ]
    }

    // Capture quality runs against the faces already found, so its results
    // stay in step with them one for one.
    var qualities = [Double](repeating: -1, count: faceObservations.count)
    if !faceObservations.isEmpty {
        let quality = VNDetectFaceCaptureQualityRequest()
        quality.inputFaceObservations = faceObservations
        if (try? handler.perform([quality])) != nil {
            for (index, observation) in (quality.results ?? []).enumerated()
            where index < qualities.count {
                if let value = observation.faceCaptureQuality {
                    qualities[index] = Double(value)
                }
            }
        }
    }

    var cullFaces: [[String: Any]] = []
    for (index, observation) in faceObservations.enumerated() {
        let box = observation.boundingBox
        var entry: [String: Any] = [
            "box": topDownBox(box),
            "quality": qualities[index],
            "leftEye": eyeContour(observation.landmarks?.leftEye, face: box),
            "rightEye": eyeContour(observation.landmarks?.rightEye, face: box),
        ]
        if let roll = observation.roll { entry["roll"] = roll.doubleValue }
        if let yaw = observation.yaw { entry["yaw"] = yaw.doubleValue }
        cullFaces.append(entry)
    }

    var cull: [String: Any] = [
        "textCoverage": min(1.0, textCoverage),
        "textLines": textLines,
        "faces": cullFaces,
    ]
    if #available(macOS 14.0, *),
       let subject = coarseSubjectMask(handler, edge: 64) {
        cull["subject"] = subject
    }

    return ["caption": "", "tags": tags, "ocr": Array(text.prefix(40)),
            "faces": faces, "cull": cull]
}

@available(macOS 14.0, *)
private func writeForegroundMask(sourceURL: URL, outputURL: URL) throws {
    let request = VNGenerateForegroundInstanceMaskRequest()
    let handler = VNImageRequestHandler(url: sourceURL, options: [:])
    try handler.perform([request])
    guard let observation = request.results?.first else {
        throw HelperFailure.message("No foreground subject found")
    }
    let buffer = try observation.generateScaledMaskForImage(
        forInstances: observation.allInstances, from: handler)
    var image = CIImage(cvPixelBuffer: buffer)
    let scale = min(1.0, 1024.0 / max(image.extent.width, image.extent.height))
    if scale < 1.0 {
        image = image.applyingFilter("CILanczosScaleTransform", parameters: [
            kCIInputScaleKey: scale,
            kCIInputAspectRatioKey: 1.0,
        ])
    }
    let context = CIContext(options: [.useSoftwareRenderer: false])
    try context.writePNGRepresentation(
        of: image, to: outputURL, format: .L8,
        colorSpace: CGColorSpaceCreateDeviceGray(), options: [:])
}

private func writeDepthMap(sourceURL: URL, outputURL: URL) throws {
    guard let source = CGImageSourceCreateWithURL(sourceURL as CFURL, nil) else {
        throw HelperFailure.message("Could not decode image")
    }
    var disparity = true
    var auxiliary = CGImageSourceCopyAuxiliaryDataInfoAtIndex(
        source, 0, kCGImageAuxiliaryDataTypeDisparity)
    if auxiliary == nil {
        disparity = false
        auxiliary = CGImageSourceCopyAuxiliaryDataInfoAtIndex(
            source, 0, kCGImageAuxiliaryDataTypeDepth)
    }
    guard let dictionary = auxiliary as? [AnyHashable: Any] else {
        throw HelperFailure.message("Image has no embedded depth data")
    }
    var depth = try AVDepthData(fromDictionaryRepresentation: dictionary)
    if let properties = CGImageSourceCopyPropertiesAtIndex(
        source, 0, nil) as? [CFString: Any],
       let raw = (properties[kCGImagePropertyOrientation] as? NSNumber)?.uint32Value,
       let orientation = CGImagePropertyOrientation(rawValue: raw) {
        depth = depth.applyingExifOrientation(orientation)
    }
    depth = depth.converting(toDepthDataType: disparity
        ? kCVPixelFormatType_DisparityFloat32
        : kCVPixelFormatType_DepthFloat32)
    let buffer = depth.depthDataMap
    CVPixelBufferLockBaseAddress(buffer, .readOnly)
    defer { CVPixelBufferUnlockBaseAddress(buffer, .readOnly) }
    guard let base = CVPixelBufferGetBaseAddress(buffer) else {
        throw HelperFailure.message("Embedded depth data is unreadable")
    }
    let width = CVPixelBufferGetWidth(buffer)
    let height = CVPixelBufferGetHeight(buffer)
    let stride = CVPixelBufferGetBytesPerRow(buffer) / MemoryLayout<Float>.stride
    let pointer = base.assumingMemoryBound(to: Float.self)
    var finite: [Float] = []
    finite.reserveCapacity(width * height)
    for y in 0..<height {
        for x in 0..<width {
            let value = pointer[y * stride + x]
            if value.isFinite && value > 0 { finite.append(value) }
        }
    }
    guard finite.count >= 16 else {
        throw HelperFailure.message("Embedded depth data contains no usable samples")
    }
    finite.sort()
    let low = finite[min(finite.count - 1, finite.count * 2 / 100)]
    let high = finite[min(finite.count - 1, finite.count * 98 / 100)]
    let span = max(high - low, Float.ulpOfOne)
    var output = [UInt8](repeating: 0, count: width * height)
    for y in 0..<height {
        for x in 0..<width {
            let value = pointer[y * stride + x]
            guard value.isFinite && value > 0 else { continue }
            let normalized = min(1, max(0, (value - low) / span))
            let nearness = disparity ? normalized : 1 - normalized
            output[y * width + x] = UInt8(round(nearness * 255))
        }
    }
    try writeMask(output, width: width, height: height, outputURL: outputURL)
}

private let personPartNames = [
    "person", "face-skin", "eyes", "eyebrows", "lips", "teeth", "hair",
]

private func maskContext(width: Int, height: Int) throws -> CGContext {
    guard let context = CGContext(
        data: nil, width: width, height: height, bitsPerComponent: 8,
        bytesPerRow: width, space: CGColorSpaceCreateDeviceGray(),
        bitmapInfo: CGImageAlphaInfo.none.rawValue) else {
        throw HelperFailure.message("Could not allocate a person-part mask")
    }
    context.setShouldAntialias(true)
    context.setAllowsAntialiasing(true)
    context.setFillColor(gray: 1, alpha: 1)
    context.setStrokeColor(gray: 1, alpha: 1)
    return context
}

private func maskBytes(_ context: CGContext, width: Int,
                       height: Int) -> [UInt8] {
    let count = width * height
    guard let data = context.data else { return [UInt8](repeating: 0, count: count) }
    return Array(UnsafeBufferPointer(
        start: data.bindMemory(to: UInt8.self, capacity: count), count: count))
}

private func writeMask(_ bytes: [UInt8], width: Int, height: Int,
                       outputURL: URL) throws {
    var mutable = bytes
    try mutable.withUnsafeMutableBytes { pointer in
        guard let context = CGContext(
            data: pointer.baseAddress, width: width, height: height,
            bitsPerComponent: 8, bytesPerRow: width,
            space: CGColorSpaceCreateDeviceGray(),
            bitmapInfo: CGImageAlphaInfo.none.rawValue),
              let image = context.makeImage() else {
            throw HelperFailure.message("Could not encode a person-part mask")
        }
        let ci = CIImage(cgImage: image)
        let renderer = CIContext(options: [.useSoftwareRenderer: false])
        try renderer.writePNGRepresentation(
            of: ci, to: outputURL, format: .L8,
            colorSpace: CGColorSpaceCreateDeviceGray(), options: [:])
    }
}

private func landmarkPoints(_ region: VNFaceLandmarkRegion2D?,
                            face: CGRect, width: Int,
                            height: Int) -> [CGPoint] {
    guard let region else { return [] }
    return region.normalizedPoints.map { point in
        CGPoint(x: (face.minX + CGFloat(point.x) * face.width) * CGFloat(width),
                y: (face.minY + CGFloat(point.y) * face.height) * CGFloat(height))
    }
}

private func fillPolygon(_ points: [CGPoint], in context: CGContext,
                         grow: CGFloat = 0) {
    guard points.count >= 3 else { return }
    context.beginPath()
    context.move(to: points[0])
    for point in points.dropFirst() { context.addLine(to: point) }
    context.closePath()
    context.fillPath()
    if grow > 0 {
        context.setLineWidth(grow * 2)
        context.setLineJoin(.round)
        context.beginPath()
        context.move(to: points[0])
        for point in points.dropFirst() { context.addLine(to: point) }
        context.closePath()
        context.strokePath()
    }
}

@available(macOS 14.0, *)
private func writePersonParts(sourceURL: URL, outputDirectory: URL) throws
    -> [String: Any] {
    // ImageIO reliably materializes the embedded/developed image for RAW
    // formats that Core Image cannot render directly. Vision receives a
    // bounded image; masks retain up to 1024 pixels on their longest edge.
    guard let imageSource = CGImageSourceCreateWithURL(
        sourceURL as CFURL, nil),
          CGImageSourceGetCount(imageSource) > 0 else {
        throw HelperFailure.message("Could not decode image")
    }
    let options: [CFString: Any] = [
        kCGImageSourceCreateThumbnailFromImageAlways: true,
        kCGImageSourceCreateThumbnailWithTransform: true,
        kCGImageSourceThumbnailMaxPixelSize: 2048,
    ]
    guard let inferenceImage = CGImageSourceCreateThumbnailAtIndex(
        imageSource, 0, options as CFDictionary) else {
        throw HelperFailure.message("Could not render image for people masks")
    }
    let outputScale = min(1.0, 1024.0 / Double(max(
        inferenceImage.width, inferenceImage.height)))
    let width = max(1, Int(round(Double(inferenceImage.width) * outputScale)))
    let height = max(1, Int(round(Double(inferenceImage.height) * outputScale)))
    let renderer = CIContext(options: [.useSoftwareRenderer: false])

    var people = VNGeneratePersonSegmentationRequest()
    people.qualityLevel = .accurate
    people.outputPixelFormat = kCVPixelFormatType_OneComponent8
    var faces = VNDetectFaceLandmarksRequest()
    let handler = VNImageRequestHandler(cgImage: inferenceImage, options: [:])
    do {
        try handler.perform([people, faces])
    } catch {
        // Some Macs reject the bundled person-segmentation ANE plan even
        // though Vision's CPU implementation is available. Preserve the
        // normal hardware path, then make the on-device fallback explicit.
        people = VNGeneratePersonSegmentationRequest()
        people.qualityLevel = .accurate
        people.outputPixelFormat = kCVPixelFormatType_OneComponent8
        people.usesCPUOnly = true
        faces = VNDetectFaceLandmarksRequest()
        faces.usesCPUOnly = true
        let fallback = VNImageRequestHandler(
            cgImage: inferenceImage, options: [:])
        try fallback.perform([people, faces])
    }

    let personContext = try maskContext(width: width, height: height)
    if let observation = people.results?.first {
        let mask = CIImage(cvPixelBuffer: observation.pixelBuffer)
        if let image = renderer.createCGImage(mask, from: mask.extent) {
            personContext.interpolationQuality = .high
            personContext.draw(image, in: CGRect(x: 0, y: 0,
                                                  width: width, height: height))
        }
    }

    let skinContext = try maskContext(width: width, height: height)
    let eyeContext = try maskContext(width: width, height: height)
    let browContext = try maskContext(width: width, height: height)
    let lipContext = try maskContext(width: width, height: height)
    let teethContext = try maskContext(width: width, height: height)
    let hairContext = try maskContext(width: width, height: height)

    let observations = faces.results ?? []
    for observation in observations {
        let box = observation.boundingBox
        let face = CGRect(x: box.minX, y: box.minY,
                          width: box.width, height: box.height)
        let pixelFace = CGRect(x: face.minX * CGFloat(width),
                               y: face.minY * CGFloat(height),
                               width: face.width * CGFloat(width),
                               height: face.height * CGFloat(height))
        let landmarks = observation.landmarks
        let leftEye = landmarkPoints(landmarks?.leftEye, face: face,
                                     width: width, height: height)
        let rightEye = landmarkPoints(landmarks?.rightEye, face: face,
                                      width: width, height: height)
        fillPolygon(leftEye, in: eyeContext, grow: 1.5)
        fillPolygon(rightEye, in: eyeContext, grow: 1.5)
        fillPolygon(landmarkPoints(landmarks?.leftEyebrow, face: face,
                                   width: width, height: height), in: browContext,
                    grow: 1.0)
        fillPolygon(landmarkPoints(landmarks?.rightEyebrow, face: face,
                                   width: width, height: height), in: browContext,
                    grow: 1.0)
        let outerLips = landmarkPoints(landmarks?.outerLips, face: face,
                                       width: width, height: height)
        let innerLips = landmarkPoints(landmarks?.innerLips, face: face,
                                       width: width, height: height)
        fillPolygon(outerLips, in: lipContext)
        lipContext.setBlendMode(.clear)
        fillPolygon(innerLips, in: lipContext)
        lipContext.setBlendMode(.normal)
        fillPolygon(innerLips, in: teethContext)

        // Face skin and hair have no dedicated Vision class. Close Vision's
        // jaw contour with a forehead cap above the brows; the masks remain
        // explicitly labelled vision-estimated by the caller.
        let contour = landmarkPoints(landmarks?.faceContour, face: face,
                                     width: width, height: height)
        let brows = landmarkPoints(landmarks?.leftEyebrow, face: face,
                                   width: width, height: height)
            + landmarkPoints(landmarks?.rightEyebrow, face: face,
                             width: width, height: height)
        if contour.count >= 3, let chinY = contour.map({ $0.y }).min() {
            let browY = brows.isEmpty
                ? pixelFace.minY + pixelFace.height * 0.62
                : brows.map({ $0.y }).reduce(0, +) / CGFloat(brows.count)
            let capY = min(CGFloat(height),
                           browY + max(0, browY - chinY) * 0.55)
            var skinPolygon = contour
            if let last = contour.last, let first = contour.first {
                skinPolygon.append(CGPoint(x: last.x, y: capY))
                skinPolygon.append(CGPoint(x: first.x, y: capY))
            }
            fillPolygon(skinPolygon, in: skinContext)
        } else {
            let skinBox = pixelFace.insetBy(dx: pixelFace.width * 0.08,
                                            dy: pixelFace.height * 0.03)
            skinContext.fillEllipse(in: skinBox)
        }
        let hairBox = CGRect(x: pixelFace.minX - pixelFace.width * 0.3,
                             y: pixelFace.midY,
                             width: pixelFace.width * 1.6,
                             height: pixelFace.height * 1.1)
        hairContext.fillEllipse(in: hairBox)
    }

    let person = maskBytes(personContext, width: width, height: height)
    let eyes = maskBytes(eyeContext, width: width, height: height)
    let brows = maskBytes(browContext, width: width, height: height)
    let lips = maskBytes(lipContext, width: width, height: height)
    let teeth = maskBytes(teethContext, width: width, height: height)
    var skin = maskBytes(skinContext, width: width, height: height)
    var hair = maskBytes(hairContext, width: width, height: height)
    for index in 0..<person.count {
        skin[index] = min(skin[index], person[index])
        if eyes[index] > 0 || brows[index] > 0 || lips[index] > 0 || teeth[index] > 0 {
            skin[index] = 0
        }
        hair[index] = (skin[index] == 0 && eyes[index] == 0 &&
                       brows[index] == 0 && lips[index] == 0 &&
                       teeth[index] == 0) ? min(hair[index], person[index]) : 0
    }
    let masks: [String: [UInt8]] = [
        "person": person, "face-skin": skin, "eyes": eyes,
        "eyebrows": brows, "lips": lips, "teeth": teeth, "hair": hair,
    ]
    try FileManager.default.createDirectory(
        at: outputDirectory, withIntermediateDirectories: true)
    var nonzero: [String: Bool] = [:]
    for name in personPartNames {
        let values = masks[name] ?? [UInt8](repeating: 0, count: width * height)
        nonzero[name] = values.contains(where: { $0 != 0 })
        try writeMask(values, width: width, height: height,
                      outputURL: outputDirectory.appendingPathComponent("\(name).png"))
    }
    return ["faces": observations.count, "parts": nonzero,
            "provider": "vision"]
}

private func handle(_ request: [String: Any]) throws -> [String: Any] {
    guard let command = request["command"] as? String,
          let input = request["input"] as? String else {
        throw HelperFailure.message("Missing Vision command or input")
    }
    let source = try checkedURL(input, kind: "Image")
    if command == "analyze" {
        return try analyzeImage(source)
    }
    if command == "foreground-mask" {
        guard #available(macOS 14.0, *) else {
            throw HelperFailure.message("Foreground masks require macOS 14 or later")
        }
        guard let output = request["output"] as? String else {
            throw HelperFailure.message("Missing foreground-mask output")
        }
        try writeForegroundMask(
            sourceURL: source, outputURL: URL(fileURLWithPath: output))
        return [:]
    }
    if command == "depth-map" {
        guard let output = request["output"] as? String else {
            throw HelperFailure.message("Missing depth-map output")
        }
        do {
            try writeDepthMap(
                sourceURL: source, outputURL: URL(fileURLWithPath: output))
            return ["available": true]
        } catch {
            // Most photos legitimately have no auxiliary depth plane. Report
            // that as an ordinary capability miss so the persistent helper is
            // not restarted before the local visual-cue fallback runs.
            return ["available": false]
        }
    }
    if command == "person-parts" {
        guard #available(macOS 14.0, *) else {
            throw HelperFailure.message(
                "People masks need the Vision helper (macOS 14 or later)")
        }
        guard let output = request["outputDir"] as? String else {
            throw HelperFailure.message("Missing person-parts output directory")
        }
        return try writePersonParts(
            sourceURL: source, outputDirectory: URL(fileURLWithPath: output))
    }
    throw HelperFailure.message("Unknown Vision command")
}

private func serve() -> Never {
    while let line = readLine(strippingNewline: true) {
        var response: [String: Any] = [:]
        do {
            guard let data = line.data(using: .utf8),
                  let request = try JSONSerialization.jsonObject(with: data)
                    as? [String: Any] else {
                throw HelperFailure.message("Invalid JSON request")
            }
            response = try handle(request)
            response["id"] = request["id"] ?? 0
            response["ok"] = true
        } catch {
            response["ok"] = false
            response["error"] = error.localizedDescription
            if let data = line.data(using: .utf8),
               let request = try? JSONSerialization.jsonObject(with: data)
                    as? [String: Any] {
                response["id"] = request["id"] ?? 0
            } else {
                response["id"] = 0
            }
        }
        do { try writeJSON(response) } catch { exit(1) }
    }
    exit(0)
}

private func writeVideoThumbnail(sourceURL: URL, outputURL: URL) throws -> Double {
    let asset = AVURLAsset(url: sourceURL)
    let generator = AVAssetImageGenerator(asset: asset)
    generator.appliesPreferredTrackTransform = true
    generator.maximumSize = CGSize(width: 1024, height: 1024)
    let time = CMTime(seconds: 1.0, preferredTimescale: 600)
    let cgImage = try generator.copyCGImage(at: time, actualTime: nil)
    let image = CIImage(cgImage: cgImage)
    let context = CIContext(options: [.useSoftwareRenderer: false])
    guard let colorSpace = CGColorSpace(name: CGColorSpace.sRGB) else {
        throw HelperFailure.message("Could not create an sRGB colour space")
    }
    try context.writeJPEGRepresentation(
        of: image, to: outputURL, colorSpace: colorSpace, options: [:])
    let seconds = CMTimeGetSeconds(asset.duration)
    return seconds.isFinite ? seconds : 0
}

private func writeHEIF(sourceURL: URL, outputURL: URL,
                       quality: Double) throws {
    guard let source = CGImageSourceCreateWithURL(sourceURL as CFURL, nil),
          let destination = CGImageDestinationCreateWithURL(
            outputURL as CFURL, "public.heic" as CFString, 1, nil) else {
        throw HelperFailure.message("Could not create HEIF encoder")
    }
    let properties = [
        kCGImageDestinationLossyCompressionQuality:
            min(1, max(0.01, quality)) as CFNumber,
    ] as CFDictionary
    CGImageDestinationAddImageFromSource(destination, source, 0, properties)
    guard CGImageDestinationFinalize(destination) else {
        throw HelperFailure.message("Could not finalize HEIF image")
    }
}

let arguments = CommandLine.arguments
if arguments.count == 2 && arguments[1] == "--server" {
    serve()
}

if arguments.count == 4 && arguments[1] == "--video-thumbnail" {
    do {
        let source = try checkedURL(arguments[2], kind: "Video")
        let duration = try writeVideoThumbnail(
            sourceURL: source, outputURL: URL(fileURLWithPath: arguments[3]))
        try writeJSON(["ok": true, "duration": duration])
        exit(0)
    } catch { fail("Video thumbnail failed: \(error.localizedDescription)") }
}

if arguments.count == 4 && arguments[1] == "--foreground-mask" {
    guard #available(macOS 14.0, *) else {
        fail("Foreground masks require macOS 14 or later")
    }
    do {
        let source = try checkedURL(arguments[2], kind: "Image")
        try writeForegroundMask(
            sourceURL: source, outputURL: URL(fileURLWithPath: arguments[3]))
        try writeJSON(["ok": true])
        exit(0)
    } catch { fail("Foreground mask failed: \(error.localizedDescription)") }
}

if arguments.count == 4 && arguments[1] == "--depth-map" {
    do {
        let source = try checkedURL(arguments[2], kind: "Image")
        try writeDepthMap(
            sourceURL: source, outputURL: URL(fileURLWithPath: arguments[3]))
        try writeJSON(["ok": true])
        exit(0)
    } catch { fail("Depth map failed: \(error.localizedDescription)") }
}

if arguments.count == 4 && arguments[1] == "--person-parts" {
    guard #available(macOS 14.0, *) else {
        fail("People masks need the Vision helper (macOS 14 or later)")
    }
    do {
        let source = try checkedURL(arguments[2], kind: "Image")
        let result = try writePersonParts(
            sourceURL: source,
            outputDirectory: URL(fileURLWithPath: arguments[3]))
        try writeJSON(["ok": true].merging(result) { _, new in new })
        exit(0)
    } catch { fail("People masks failed: \(error.localizedDescription)") }
}

if arguments.count == 5 && arguments[1] == "--encode-heif" {
    do {
        let source = try checkedURL(arguments[2], kind: "Image")
        let quality = (Double(arguments[4]) ?? 92) / 100
        try writeHEIF(
            sourceURL: source, outputURL: URL(fileURLWithPath: arguments[3]),
            quality: quality)
        try writeJSON(["ok": true])
        exit(0)
    } catch { fail("HEIF export failed: \(error.localizedDescription)") }
}

guard arguments.count == 2 else {
    fail("Usage: LightTableVision <image> | --server"
         + " | --foreground-mask <image> <output.png>"
         + " | --depth-map <image> <output.png>"
         + " | --person-parts <image> <output-directory>"
         + " | --encode-heif <image> <output.heic> <quality>"
         + " | --video-thumbnail <video> <output.jpg>")
}

do {
    let source = try checkedURL(arguments[1], kind: "Image")
    try writeJSON(analyzeImage(source))
} catch {
    fail("Vision analysis failed: \(error.localizedDescription)")
}
