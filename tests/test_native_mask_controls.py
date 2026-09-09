"""Replay local-slider messages through Metal without opening a native window."""
import json
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

import edits

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == 'darwin' and shutil.which('swiftc'), 'requires macOS Metal')
class NativeMaskControlTests(unittest.TestCase):
    def test_exposure_messages_change_only_masked_pixels_without_reuploading_geometry(self):
        import base64
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            contents = temporary / 'MaskControls.app/Contents'
            resources = contents / 'Resources'
            resources.mkdir(parents=True)
            executable = contents / 'MacOS/MaskControls'
            executable.parent.mkdir()
            (contents / 'Info.plist').write_bytes(plistlib.dumps({
                'CFBundleExecutable': 'MaskControls',
                'CFBundleIdentifier': 'com.lighttable.test.mask-controls',
                'CFBundlePackageType': 'APPL',
                'LSBackgroundOnly': True,
            }))
            shutil.copy(ROOT / 'app/NativePreview.metal', resources)
            Image.open(ROOT / 'tests/fixtures/photos/field.jpg').convert('RGB').resize((128, 96)).save(
                temporary / 'photo.png')
            fixture = subprocess.run(['node', str(ROOT / 'tests/native-mask-controls.test.mjs'),
                                      '--fixtures'], capture_output=True, text=True, check=True, timeout=10)
            frames = json.loads(fixture.stdout)
            for frame in frames:
                mask_updates = [m['payload'] for m in frame['messages'] if m['action'] == 'nativeMasks']
                self.assertEqual(len(mask_updates), 1, 'each exposure change must reach Metal')
                self.assertNotIn('data', mask_updates[0], 'reuse the existing mask texture')
            (temporary / 'frames.json').write_text(json.dumps(frames))
            values = edits.raster_mask({'type': 'linear', 'start': [.25, .5], 'end': [.75, .5]}, 96, 128)
            atlas = np.zeros((96, 128, 4), dtype=np.uint8)
            atlas[:, :, 0] = np.rint(values * 255).astype(np.uint8)
            (temporary / 'atlas.json').write_text(json.dumps({'width': 128, 'height': 96,
                'data': base64.b64encode(atlas.tobytes()).decode(), 'masks': []}))
            # Test-only fixture loading leaves production rendering and mask
            # update methods intact. No NSApplication or NSWindow is created.
            source = temporary / 'NativePreview.swift'
            source.write_text((ROOT / 'app/NativePreview.swift').read_text() + FIXTURE_EXTENSION)
            main = temporary / 'MaskControls.swift'
            main.write_text(HARNESS)
            compiled = subprocess.run(['swiftc', '-swift-version', '5', '-module-cache-path',
                str(temporary / 'module-cache'), str(source), str(main), '-o', str(executable)],
                capture_output=True, text=True, timeout=90)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            result = subprocess.run([str(executable), str(temporary)],
                                    capture_output=True, text=True, timeout=30)
            if result.returncode == 77:
                self.skipTest(result.stderr.strip())
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            images = [np.asarray(Image.open(temporary / f'{i}.png').convert('RGB')).astype(np.int16)
                      for i in range(4)]
            baseline, brighter, darker, reset = images
            np.testing.assert_array_equal(reset, baseline, 'reset must restore the exact original pixels')
            for image in [brighter, darker]:
                np.testing.assert_array_equal(image[:, :20], baseline[:, :20],
                                              'pixels outside the gradient must remain unchanged')
            lift = float((brighter[:, 108:] - baseline[:, 108:]).mean())
            drop = float((baseline[:, 108:] - darker[:, 108:]).mean())
            self.assertGreater(lift, 8, 'positive local exposure must visibly brighten the selected area')
            self.assertGreater(drop, 8, 'negative local exposure must visibly darken the selected area')
            print(f'Metal mask pixels: +1 EV lifts by {lift:.1f}/255; -1 EV lowers by {drop:.1f}/255; '
                  'unmasked pixels and reset exact. No native window opened.')


FIXTURE_EXTENSION = r'''
extension NativePreviewRenderer {
    func loadMaskControlFixture(_ url: URL) throws {
        imageTexture = try MTKTextureLoader(device: device).newTexture(URL: url,
            options: [.SRGB: false, .origin: MTKTextureLoader.Origin.topLeft])
        view.frame = NSRect(x: 0, y: 0, width: 128, height: 96)
        view.isHidden = false
    }
}
'''

HARNESS = r'''
import AppKit
import MetalKit

@main
struct MaskControls {
    static func main() throws {
        guard MTLCreateSystemDefaultDevice() != nil else {
            fputs("Metal device unavailable\n", stderr); exit(77)
        }
        guard let renderer = NativePreviewRenderer() else {
            fputs("Could not initialize the production Metal renderer\n", stderr); exit(1)
        }
        let root = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
        try renderer.loadMaskControlFixture(root.appendingPathComponent("photo.png"))
        let atlas = try JSONSerialization.jsonObject(with: Data(contentsOf:
            root.appendingPathComponent("atlas.json"))) as! [String: Any]
        renderer.updateMasks(atlas)
        let frames = try JSONSerialization.jsonObject(with: Data(contentsOf:
            root.appendingPathComponent("frames.json"))) as! [[String: Any]]
        for (index, frame) in frames.enumerated() {
            for message in frame["messages"] as! [[String: Any]] {
                let payload = message["payload"] as! [String: Any]
                if message["action"] as! String == "nativeMasks" {
                    renderer.updateMasks(payload)
                } else {
                    renderer.updateGrade(payload["grade"] as! [String: Any])
                }
            }
            guard let image = renderer.snapshot(), let tiff = image.tiffRepresentation,
                  let bitmap = NSBitmapImageRep(data: tiff),
                  let png = bitmap.representation(using: .png, properties: [:]) else {
                fputs("Native offscreen rendering failed\n", stderr); exit(1)
            }
            try png.write(to: root.appendingPathComponent("\(index).png"))
        }
    }
}
'''
