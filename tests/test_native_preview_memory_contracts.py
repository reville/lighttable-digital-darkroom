# SPDX-License-Identifier: GPL-3.0-only
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == "darwin", "Metal contracts are macOS-only")
class NativePreviewMemoryContractTests(unittest.TestCase):
    def test_raw_surface_pool_requires_the_complete_texture_layout(self):
        swiftc = shutil.which("swiftc")
        if not swiftc:
            self.skipTest("swiftc is unavailable")
        harness = textwrap.dedent(
            """
            import MetalKit

            @main
            struct NativePreviewMemoryContractHarness {
                static func require(_ condition: @autoclosure () -> Bool,
                                    _ message: String) {
                    if !condition() { fatalError(message) }
                }

                static func main() {
                    let cache = ByteBudgetCache<String>(budget: 10, limit: 3)
                    cache.insert("A", key: "a", cost: 4)
                    cache.insert("B", key: "b", cost: 4)
                    require(cache.value(for: "a") == "A", "cache hit")
                    cache.insert("C", key: "c", cost: 4)
                    require(cache.value(for: "b") == nil, "LRU eviction")
                    require(cache.cost == 8, "exact byte accounting")
                    cache.insert("large", key: "large", cost: 11)
                    require(cache.cost == 8, "oversized values bypass retention")
                    cache.insert("replacement", key: "a", cost: 6)
                    require(cache.cost == 10, "replacement accounting")
                    cache.removeAll()
                    require(cache.cost == 0 && cache.value(for: "a") == nil,
                            "memory pressure clears retained values")
                    let rgba = RawSurfaceTextureDescriptor(
                        pixelFormat: .rgba8Unorm, width: 800, height: 1070)
                    require(rgba.isReusableRawSurface(width: 800, height: 1070),
                            "the canonical raw texture must be reusable")

                    let bgra = RawSurfaceTextureDescriptor(
                        pixelFormat: .bgra8Unorm, width: 800, height: 1070)
                    require(!bgra.isReusableRawSurface(width: 800, height: 1070),
                            "decoded BGRA images must never receive RGBA bytes")

                    let privateStorage = RawSurfaceTextureDescriptor(
                        pixelFormat: .rgba8Unorm, width: 800, height: 1070,
                        storageMode: .private)
                    require(!privateStorage.isReusableRawSurface(
                        width: 800, height: 1070),
                        "CPU uploads require shared storage")

                    let mipmapped = RawSurfaceTextureDescriptor(
                        pixelFormat: .rgba8Unorm, width: 800, height: 1070,
                        mipmapLevelCount: 2)
                    require(!mipmapped.isReusableRawSurface(
                        width: 800, height: 1070),
                        "a different mip layout must not enter the pool")

                    require(!rgba.isReusableRawSurface(width: 801, height: 1070),
                            "dimensions remain part of the reuse contract")
                }
            }
            """
        )
        with tempfile.TemporaryDirectory() as temporary:
            temporary = Path(temporary)
            main = temporary / "NativePreviewMemoryContractHarness.swift"
            executable = temporary / "native-preview-memory-contract"
            main.write_text(harness, encoding="utf-8")
            subprocess.run(
                [
                    swiftc,
                    "-swift-version",
                    "5",
                    "-module-cache-path",
                    str(temporary / "module-cache"),
                    str(ROOT / "app" / "NativePreview.swift"),
                    str(main),
                    "-o",
                    str(executable),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run([str(executable)], check=True)

    def test_rgba_layout_rejects_invalid_and_overflowing_buffers(self):
        swift = (ROOT / "app" / "NativePreview.swift").read_text()
        self.assertIn(
            "width: targetWidth, height: maskTexture.height", swift)
        self.assertNotIn(
            "count: targetWidth * targetHeight * 4", swift)
        self.assertIn(
            "withBytes: base.advanced(by: tileByteOffset)", swift)
        self.assertIn(
            "0, tileY, targetWidth, targetHeight", swift)

        swiftc = shutil.which("swiftc")
        if not swiftc:
            self.skipTest("swiftc is unavailable")
        harness = textwrap.dedent(
            """
            import MetalKit

            @main
            struct PackedRGBA8ContractHarness {
                static func main() {
                    guard let atlas = PackedRGBA8Layout(
                        width: 512, height: 2048) else {
                        fatalError("a valid atlas layout was rejected")
                    }
                    precondition(atlas.rowBytes == 2048)
                    precondition(atlas.byteCount == 4_194_304)
                    precondition(PackedRGBA8Layout(width: 0, height: 1) == nil)
                    precondition(PackedRGBA8Layout(
                        width: Int.max, height: 2) == nil)
                }
            }
            """
        )
        with tempfile.TemporaryDirectory() as temporary:
            temporary = Path(temporary)
            main = temporary / "PackedRGBA8ContractHarness.swift"
            executable = temporary / "packed-rgba8-contract"
            main.write_text(harness, encoding="utf-8")
            subprocess.run(
                [
                    swiftc,
                    "-swift-version",
                    "5",
                    "-module-cache-path",
                    str(temporary / "module-cache"),
                    str(ROOT / "app" / "NativePreview.swift"),
                    str(main),
                    "-o",
                    str(executable),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run([str(executable)], check=True)


if __name__ == "__main__":
    unittest.main()
