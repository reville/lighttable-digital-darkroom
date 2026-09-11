// SPDX-License-Identifier: GPL-3.0-only
// Runs a Core ML denoise or super-resolution model over one image tile.
//
// `enhance_workflow.py` owns tiling, feathering, and the output master; this
// binary is the narrow part that Python cannot do, which is running a Core ML
// model on the GPU. It follows the same argv-and-subprocess convention as
// `vision_helper.swift`, so it is built, signed, and bundled the same way.
//
// Release builds bundle the denoise model; development builds may not. The
// model directory is supplied through LIGHTTABLE_MODEL_DIR and the caller
// checks it before invoking this binary. A missing model exits non-zero rather
// than passing pixels through unchanged.
//
// Usage:
//   LightTableEnhance --denoise <in.tile> <out.tile> [--strength 0.6]
//   LightTableEnhance --denoise-batch <manifest.tsv> [--strength 0.6]
//   LightTableEnhance --upscale <2|4> <in.tif> <out.tif>
//   LightTableEnhance --probe            (reports which models are installed)

import Foundation
import CoreML

private func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(1)
}

private func modelDirectory() -> URL {
    if let override = ProcessInfo.processInfo.environment["LIGHTTABLE_MODEL_DIR"] {
        return URL(fileURLWithPath: override)
    }
    let support = FileManager.default.urls(for: .applicationSupportDirectory,
                                           in: .userDomainMask)[0]
    return support.appendingPathComponent("LightTable/Models")
}

private func modelURL(_ name: String) -> URL? {
    let candidate = modelDirectory().appendingPathComponent(name)
    return FileManager.default.fileExists(atPath: candidate.path) ? candidate : nil
}

private func compiledCacheURL(for model: URL, name: String) -> URL {
    var identity = "local"
    let index = modelDirectory().appendingPathComponent("models.json")
    if let data = try? Data(contentsOf: index),
       let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
       let entry = object[name == "denoise.mlpackage" ? "denoise" : "upscale"]
            as? [String: Any],
       let digest = entry["sha256"] as? String, !digest.isEmpty {
        identity = String(digest.prefix(16))
    } else if let values = try? model.resourceValues(
        forKeys: [.contentModificationDateKey]), let date = values.contentModificationDate {
        identity = String(Int(date.timeIntervalSince1970))
    }
    let root = FileManager.default.urls(for: .cachesDirectory,
                                        in: .userDomainMask)[0]
        .appendingPathComponent("LightTable/Compiled Models", isDirectory: true)
    return root.appendingPathComponent(
        "\(URL(fileURLWithPath: name).deletingPathExtension().lastPathComponent)"
        + "-\(identity).mlmodelc", isDirectory: true)
}

private func loadModel(_ name: String) -> MLModel {
    guard let url = modelURL(name) else {
        fail("No model named \(name) in \(modelDirectory().path).")
    }
    do {
        var compiled = url
        if url.pathExtension != "mlmodelc" {
            // Keep compiled output in the user's cache. Writing it beside a
            // bundled package would mutate the signed application.
            let cached = compiledCacheURL(for: url, name: name)
            if FileManager.default.fileExists(atPath: cached.path) {
                compiled = cached
            } else {
                let temporary = try MLModel.compileModel(at: url)
                do {
                    try FileManager.default.createDirectory(
                        at: cached.deletingLastPathComponent(),
                        withIntermediateDirectories: true)
                    try? FileManager.default.removeItem(at: cached)
                    try FileManager.default.moveItem(at: temporary, to: cached)
                    compiled = cached
                } catch {
                    compiled = temporary
                }
            }
        }
        let configuration = MLModelConfiguration()
        configuration.computeUnits = .all
        return try MLModel(contentsOf: compiled, configuration: configuration)
    } catch {
        fail("Could not load \(name): \(error.localizedDescription)")
    }
}

/// Feed one tile through the model.
///
/// The model takes and returns a float multiarray, not a Core ML image. That
/// is deliberate: Core ML's image inputs are eight bits per channel, and
/// pushing a sixteen-bit raw tile through one would quantise the shadows this
/// feature exists to clean up.
private func run(model: MLModel, on tile: [Float],
                 width: Int, height: Int) -> [Float] {
    let description = model.modelDescription
    guard let inputName = description.inputDescriptionsByName.first(where: {
        $0.value.type == .multiArray
    })?.key else {
        fail("That model does not take a multiarray input")
    }
    guard let outputName = description.outputDescriptionsByName.first(where: {
        $0.value.type == .multiArray
    })?.key else {
        fail("That model does not return a multiarray")
    }

    do {
        // NCHW, planar, matching the converted graph.
        let array = try MLMultiArray(shape: [1, 3, NSNumber(value: height),
                                             NSNumber(value: width)],
                                     dataType: .float32)
        let pointer = array.dataPointer.bindMemory(
            to: Float.self, capacity: tile.count)
        tile.withUnsafeBufferPointer { source in
            pointer.update(from: source.baseAddress!, count: tile.count)
        }
        let provider = try MLDictionaryFeatureProvider(
            dictionary: [inputName: MLFeatureValue(multiArray: array)])
        let result = try model.prediction(from: provider)
        guard let out = result.featureValue(for: outputName)?.multiArrayValue
        else {
            fail("The model returned no output")
        }
        let count = out.count
        var values = [Float](repeating: 0, count: count)
        let outPointer = out.dataPointer.bindMemory(to: Float.self,
                                                    capacity: count)
        for index in 0..<count {
            values[index] = outPointer[index]
        }
        return values
    } catch {
        fail("Model run failed: \(error.localizedDescription)")
    }
}

/// Tile exchange format: a bare header and planar float32 samples.
///
/// TIFF was the obvious choice and the wrong one. Core Image colour-manages
/// anything it reads, so a tile written by numpy came back through an
/// sRGB-to-linear conversion and the model was handed values that were not the
/// ones the caller meant. Measured against a known-clean reference that showed
/// up immediately as a seventeen-decibel *loss*.
///
/// These are working pixels mid-pipeline, already in the caller's space, so
/// the exchange carries no colour information at all and converts nothing:
///
///     magic "FLT0" | int32 width | int32 height | int32 channels
///     float32 samples, planar, channel-major, host byte order
private let tileMagic: [UInt8] = Array("FLT0".utf8)

private func readTile(_ path: String) -> (pixels: [Float], width: Int,
                                          height: Int) {
    guard let data = FileManager.default.contents(atPath: path) else {
        fail("Could not read \(path)")
    }
    guard data.count >= 16, Array(data[0..<4]) == tileMagic else {
        fail("\(path) is not a LightTable tile")
    }
    let header = data.subdata(in: 4..<16).withUnsafeBytes {
        (raw: UnsafeRawBufferPointer) -> [Int32] in
        [raw.loadUnaligned(fromByteOffset: 0, as: Int32.self),
         raw.loadUnaligned(fromByteOffset: 4, as: Int32.self),
         raw.loadUnaligned(fromByteOffset: 8, as: Int32.self)]
    }
    let width = Int(header[0]), height = Int(header[1])
    let channels = Int(header[2])
    guard channels == 3 else { fail("expected 3 channels, got \(channels)") }
    let count = width * height * channels
    guard data.count >= 16 + count * MemoryLayout<Float>.size else {
        fail("\(path) is truncated")
    }
    var pixels = [Float](repeating: 0, count: count)
    _ = pixels.withUnsafeMutableBytes { destination in
        data.subdata(in: 16..<(16 + count * MemoryLayout<Float>.size))
            .copyBytes(to: destination)
    }
    return (pixels, width, height)
}

private func writeTile(_ pixels: [Float], width: Int, height: Int,
                       to path: String) {
    var data = Data(tileMagic)
    for value in [Int32(width), Int32(height), Int32(3)] {
        withUnsafeBytes(of: value) { data.append(contentsOf: $0) }
    }
    pixels.withUnsafeBufferPointer { data.append(Data(buffer: $0)) }
    do {
        try data.write(to: URL(fileURLWithPath: path), options: .atomic)
    } catch {
        fail("Could not write \(path): \(error.localizedDescription)")
    }
}

let arguments = CommandLine.arguments

if arguments.count == 2 && arguments[1] == "--probe" {
    let denoise = modelURL("denoise.mlpackage") != nil
    let upscale = modelURL("upscale.mlpackage") != nil
    let payload = "{\"denoise\":\(denoise),\"upscale\":\(upscale),"
        + "\"modelDir\":\"\(modelDirectory().path)\"}\n"
    FileHandle.standardOutput.write(Data(payload.utf8))
    exit(0)
}

// Many tiles in one process. Per-tile work is milliseconds while the model
// load is seconds, so a whole image is denoised in a single invocation.
if arguments.count >= 3 && arguments[1] == "--denoise-batch" {
    var strength: Float = 1.0
    if let index = arguments.firstIndex(of: "--strength"),
       index + 1 < arguments.count, let value = Float(arguments[index + 1]) {
        strength = max(0.0, min(1.0, value))
    }
    guard let manifest = try? String(contentsOfFile: arguments[2],
                                     encoding: .utf8) else {
        fail("Could not read the tile manifest at \(arguments[2])")
    }
    let pairs = manifest.split(separator: "\n").map {
        $0.split(separator: "\t", maxSplits: 1).map(String.init)
    }.filter { $0.count == 2 }
    if pairs.isEmpty { fail("The manifest lists no tiles") }

    let model = loadModel("denoise.mlpackage")
    var done = 0
    for pair in pairs {
        let (pixels, width, height) = readTile(pair[0])
        let denoised = run(model: model, on: pixels, width: width,
                           height: height)
        guard denoised.count == pixels.count else {
            fail("Model returned \(denoised.count) values for \(pixels.count)")
        }
        var output = denoised
        if strength < 1.0 {
            for index in 0..<output.count {
                output[index] = pixels[index] * (1 - strength)
                    + denoised[index] * strength
            }
        }
        writeTile(output, width: width, height: height, to: pair[1])
        done += 1
        if done % 8 == 0 || done == pairs.count {
            FileHandle.standardOutput.write(
                Data("{\"progress\":\(done),\"total\":\(pairs.count)}\n".utf8))
        }
    }
    FileHandle.standardOutput.write(
        Data("{\"ok\":true,\"tiles\":\(done)}\n".utf8))
    exit(0)
}

if arguments.count >= 4 && arguments[1] == "--denoise" {
    var strength: Float = 1.0
    if let index = arguments.firstIndex(of: "--strength"),
       index + 1 < arguments.count, let value = Float(arguments[index + 1]) {
        strength = max(0.0, min(1.0, value))
    }
    let (pixels, width, height) = readTile(arguments[2])
    let model = loadModel("denoise.mlpackage")
    let denoised = run(model: model, on: pixels, width: width, height: height)
    guard denoised.count == pixels.count else {
        fail("The model returned \(denoised.count) values for \(pixels.count)")
    }
    // Strength blends against the original, so the control means the same
    // thing whether or not a model exposes one of its own.
    var output = denoised
    if strength < 1.0 {
        for index in 0..<output.count {
            output[index] = pixels[index] * (1 - strength)
                + denoised[index] * strength
        }
    }
    writeTile(output, width: width, height: height, to: arguments[3])
    FileHandle.standardOutput.write(Data("{\"ok\":true}\n".utf8))
    exit(0)
}

if arguments.count >= 5 && arguments[1] == "--upscale" {
    guard let scale = Int(arguments[2]), [2, 4].contains(scale) else {
        fail("Only 2x and 4x are supported")
    }
    let (pixels, width, height) = readTile(arguments[3])
    let model = loadModel("upscale.mlpackage")
    let enlarged = run(model: model, on: pixels, width: width, height: height)
    let expected = width * scale * height * scale * 3
    guard enlarged.count == expected else {
        fail("The upscale model returned \(enlarged.count) values, expected "
             + "\(expected) for \(scale)x")
    }
    writeTile(enlarged, width: width * scale, height: height * scale,
              to: arguments[4])
    FileHandle.standardOutput.write(Data("{\"ok\":true}\n".utf8))
    exit(0)
}

fail("Usage: LightTableEnhance --denoise <in.tif> <out.tif> [--strength N]"
     + " | --denoise-batch <manifest.tsv> [--strength N]"
     + " | --upscale <2|4> <in.tif> <out.tif>"
     + " | --probe")
