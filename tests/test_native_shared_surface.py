# SPDX-License-Identifier: GPL-3.0-only
"""Exercise the real mmap-backed Metal texture and immutable lifetime contract."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("swiftc"), "requires macOS Metal")
class NativeSharedSurfaceTests(unittest.TestCase):
    def test_texture_reads_padded_rows_and_retains_mapping_after_unlink(self):
        from multiprocessing import shared_memory
        page = os.sysconf("SC_PAGE_SIZE")
        segment = shared_memory.SharedMemory(create=True, size=page, name=f"lt-{os.getpid():x}-test")
        segment.buf[:8] = bytes([0, 128, 255, 255, 255, 0, 64, 255])
        segment.buf[256:264] = bytes([64, 128, 191, 255, 0, 0, 0, 255])
        source = r'''
import Darwin
import MetalKit
@main struct SharedSurfaceContract {
    static func verifyRegionSampling(device: MTLDevice, library: MTLLibrary, shader: String) throws {
        let descriptor = MTLRenderPipelineDescriptor()
        descriptor.vertexFunction = library.makeFunction(name: "nativePreviewVertex")
        descriptor.fragmentFunction = library.makeFunction(name: "nativePreviewFragment")
        descriptor.colorAttachments[0].pixelFormat = .rgba8Unorm
        let pipeline = try device.makeRenderPipelineState(descriptor: descriptor)
        let queue = device.makeCommandQueue()!
        let samplerDescriptor = MTLSamplerDescriptor()
        samplerDescriptor.minFilter = .linear; samplerDescriptor.magFilter = .linear
        samplerDescriptor.sAddressMode = .clampToEdge; samplerDescriptor.tAddressMode = .clampToEdge
        let sampler = device.makeSamplerState(descriptor: samplerDescriptor)!
        func texture(_ width: Int, _ height: Int, _ bytes: [UInt8]) -> MTLTexture {
            let desc = MTLTextureDescriptor.texture2DDescriptor(pixelFormat: .rgba8Unorm,
                width: width, height: height, mipmapped: false)
            desc.usage = [.shaderRead, .renderTarget]; desc.storageMode = .shared
            let result = device.makeTexture(descriptor: desc)!
            bytes.withUnsafeBytes { source in
                result.replace(region: MTLRegionMake2D(0, 0, width, height), mipmapLevel: 0,
                    withBytes: source.baseAddress!, bytesPerRow: width * 4)
            }
            return result
        }
        var pixels = [UInt8]()
        for index in 0..<16 { pixels += [UInt8(index * 13), UInt8(200-index*7), UInt8(index*9), 255] }
        let full = texture(4, 4, pixels)
        let cropPixels = Array(pixels[20..<28]) + Array(pixels[36..<44])
        let tile = texture(2, 2, cropPixels)
        let placeholder = texture(1, 1, [0, 0, 0, 0])
        let structStart = shader.range(of: "struct GradeUniforms {")!.upperBound
        let structEnd = shader.range(of: "};", range: structStart..<shader.endIndex)!.lowerBound
        let fields = String(shader[structStart..<structEnd]).split(separator: "\n").compactMap { line -> String? in
            let text = line.trimmingCharacters(in: .whitespaces)
            guard text.hasPrefix("float4 ") else { return nil }
            return String(text.dropFirst(7).prefix(while: { $0 != ";" }))
        }
        func render(_ image: MTLTexture, region: [Float]) -> [UInt8] {
            var uniforms = [Float](repeating: 0, count: fields.count * 4)
            func set(_ name: String, _ value: [Float]) {
                let start = fields.firstIndex(of: name)! * 4
                uniforms.replaceSubrange(start..<start+4, with: value)
            }
            set("viewport", [1, 1, 0, 0]); set("sourceRegion", region)
            set("tone3", [0, 0.2, 0.25, 0.25]); set("optics1", [1, 0, 0, 1])
            set("tone2", [0, 0, 0.2, 0.1]); set("detail0", [0.1, 1, 0.25, 0])
            let target = texture(4, 4, [UInt8](repeating: 0, count: 64))
            let pass = MTLRenderPassDescriptor()
            pass.colorAttachments[0].texture = target
            pass.colorAttachments[0].loadAction = .clear; pass.colorAttachments[0].storeAction = .store
            let command = queue.makeCommandBuffer()!
            let encoder = command.makeRenderCommandEncoder(descriptor: pass)!
            encoder.setRenderPipelineState(pipeline)
            encoder.setFragmentTexture(image, index: 0); encoder.setFragmentTexture(placeholder, index: 1)
            encoder.setFragmentTexture(full, index: 2); encoder.setFragmentTexture(placeholder, index: 3)
            encoder.setFragmentTexture(placeholder, index: 4); encoder.setFragmentTexture(full, index: 5)
            encoder.setFragmentSamplerState(sampler, index: 0)
            uniforms.withUnsafeBytes { encoder.setFragmentBytes($0.baseAddress!, length: $0.count, index: 0) }
            let blank = [Float](repeating: 0, count: 32)
            blank.withUnsafeBytes {
                encoder.setFragmentBytes($0.baseAddress!, length: $0.count, index: 1)
                encoder.setFragmentBytes($0.baseAddress!, length: $0.count, index: 2)
            }
            encoder.drawPrimitives(type: .triangleStrip, vertexStart: 0, vertexCount: 4)
            encoder.endEncoding(); command.commit(); command.waitUntilCompleted()
            precondition(command.error == nil, "region sampling render failed")
            var output = [UInt8](repeating: 0, count: 64)
            target.getBytes(&output, bytesPerRow: 16, from: MTLRegionMake2D(0, 0, 4, 4), mipmapLevel: 0)
            return output
        }
        let baseline = render(full, region: [1, 1, 0, 0])
        let region = render(tile, region: [0.5, 0.5, 0.25, 0.25])
        precondition(region == baseline, "tile plus fallback changed full-frame grade or neighbor sampling")
        precondition(Set(region).count > 10, "render proof contains no image detail")
    }

    static func main() throws {
        guard let device = MTLCreateSystemDefaultDevice() else { exit(77) }
        let shader = try String(contentsOfFile: CommandLine.arguments[3], encoding: .utf8)
        let library = try device.makeLibrary(source: shader, options: nil)
        try verifyRegionSampling(device: device, library: library, shader: shader)
        let length = Int(CommandLine.arguments[2])!
        let description: [String: Any] = ["name": CommandLine.arguments[1],
            "length": length, "rowBytes": 256, "offset": 0]
        let surface = SharedNativeSurface(payload: description)!
        let texture = try surface.texture(device: device, width: 2, height: 2)
        precondition(texture.buffer != nil, "must retain the shared backing buffer")
        precondition(shm_unlink(surface.name) == 0)
        var pixels = [UInt8](repeating: 0, count: 16)
        texture.getBytes(&pixels, bytesPerRow: 8,
            from: MTLRegionMake2D(0, 0, 2, 2), mipmapLevel: 0)
        precondition(pixels == [0,128,255,255,255,0,64,255,64,128,191,255,0,0,0,255])
        precondition(SharedNativeSurface(payload: ["name": "/untrusted", "length": length,
            "rowBytes": 256]) == nil)
        precondition(SharedNativeSurface(payload: ["name": surface.name, "length": length,
            "rowBytes": 255]) == nil)
        do {
            _ = try surface.texture(device: device, width: 128, height: 2)
            fatalError("undersized row was accepted")
        } catch {}
    }
}
'''
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                harness = root / "SharedSurfaceContract.swift"
                executable = root / "shared-surface-contract"
                harness.write_text(source)
                subprocess.run(["swiftc", "-swift-version", "5", "-module-cache-path",
                                str(root / "module-cache"), str(ROOT / "app/NativePreview.swift"),
                                str(harness), "-o", str(executable)], check=True,
                               capture_output=True, text=True)
                result = subprocess.run([str(executable), "/" + segment.name, str(page),
                                         str(ROOT / "app/NativePreview.metal")],
                                        capture_output=True, text=True)
                if result.returncode == 77:
                    self.skipTest("Metal device unavailable")
                self.assertEqual(result.returncode, 0, result.stderr)
        finally:
            segment.close()
            try:
                segment.unlink()
            except FileNotFoundError:
                # The Swift harness owns the explicit unlink lifetime check.
                from multiprocessing import resource_tracker
                resource_tracker.unregister(segment._name, "shared_memory")

    def test_viewport_window_is_bounded_and_accounts_for_pan(self):
        script = (ROOT / "web/app.js").read_text()
        start = script.index("function viewportPixelWindow(")
        end = script.index("\nlet viewportRegionTimer", start)
        result = subprocess.run(["node", "-e", script[start:end] + r'''
const assert = require('node:assert/strict');
const clip = {left:0,top:0,right:600,bottom:400};
const full = {left:-1200,top:-900,right:1800,bottom:1100,width:3000,height:2000};
const region = viewportPixelWindow(full,clip,3000,2000);
assert.ok(region.x <= 1200 && region.y <= 900);
assert.ok(region.x+region.width >= 1800 && region.y+region.height >= 1300);
assert.ok(region.width < 900 && region.height < 700);
const corner = viewportPixelWindow({left:0,top:0,right:3000,bottom:2000,width:3000,height:2000},clip,3000,2000);
assert.equal(corner.x,0); assert.equal(corner.y,0);
assert.equal(viewportPixelWindow({...full,width:0},clip,3000,2000),null);
const all = viewportPixelWindow({left:0,top:0,right:400,bottom:300,width:400,height:300},clip,400,300);
assert.deepEqual(all,{x:0,y:0,width:400,height:300});
// Native ROI always uses original source pixels, even beyond the old 8000px
// full-frame preview cap.
function viewportRegionEnabled() { return true; }
function cur() { return {width:10000,height:6000}; }
function requestedPreviewWidth() { return 8000; }
const S = {params:{rotate:0}};
function $(id) { return {getBoundingClientRect: () => id === 'cv'
  ? {left:-4000,top:-2000,right:6000,bottom:4000,width:10000,height:6000}
  : clip}; }
assert.equal(requestedViewportRegion().x,3904);
S.viewportSourceGeometry = {key:viewportSourceGeometryKey(),width:12000,height:7200};
assert.equal(requestedViewportRegion().x,4672);
S.viewportSourceGeometry.key = 'different-photo';
assert.equal(requestedViewportRegion().x,3904);
'''], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_adaptive_interval_tracks_round_trips_without_cache_feedback(self):
        script = (ROOT / "web/app.js").read_text()
        start = script.index("function adaptiveInteractiveInterval(")
        end = script.index("\nlet interactiveRenderIntervalMs", start)
        result = subprocess.run(["node", "-e", script[start:end] + r'''
const assert = require('node:assert/strict');
assert.equal(adaptiveInteractiveInterval(null, 8), 1000 / 60);
assert.equal(adaptiveInteractiveInterval(null, 60), 60);
assert.equal(adaptiveInteractiveInterval(null, 900), 250);
assert.equal(adaptiveInteractiveInterval(40, NaN), 40);
let interval = 60;
for (let i = 0; i < 20; i++) interval = adaptiveInteractiveInterval(interval, 8);
assert.ok(interval < 17);
interval = adaptiveInteractiveInterval(interval, 100);
assert.ok(interval > 40 && interval < 45);
'''], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
