"""Exercise actual macOS image pasteboard representations without changing the user's clipboard."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

@unittest.skipUnless(sys.platform == 'darwin' and shutil.which('swiftc'), 'requires macOS Swift')
class PhotoClipboardTests(unittest.TestCase):
    def test_png_and_tiff_round_trip_and_invalid_input_preserves_clipboard(self):
        source = (ROOT / 'app/main.swift').read_text().split('// MARK: - Photo clipboard\n', 1)[1].split('// MARK: - About', 1)[0]
        with tempfile.TemporaryDirectory(prefix='lighttable-photo-clipboard-') as directory:
            root = Path(directory)
            main = root / 'main.swift'
            main.write_text('import AppKit\n' + source + r'''
let board = NSPasteboard.withUniqueName()
defer { board.releaseGlobally() }
let png = Data(base64Encoded: "iVBORw0KGgoAAAANSUhEUgAAAAMAAAACCAIAAAASFvFNAAAAEElEQVR4nGP8zwAFTDAGAwATKQED8NgHhAAAAABJRU5ErkJggg==")!
precondition(writePhotoClipboard(png.base64EncodedString(), to: board))
for type in [NSPasteboard.PasteboardType.png, .tiff] {
    let decoded = NSBitmapImageRep(data: board.data(forType: type)!)!
    precondition(decoded.pixelsWide == 3 && decoded.pixelsHigh == 2)
    precondition(decoded.colorAt(x: 1, y: 1)!.usingColorSpace(.deviceRGB)!.redComponent > 0.9)
}
precondition(NSImage(pasteboard: board) != nil)
let change = board.changeCount
precondition(!writePhotoClipboard("not an image", to: board))
precondition(!writePhotoClipboard(Data("hello".utf8).base64EncodedString(), to: board))
precondition(board.changeCount == change)
print("PNG and TIFF paste successfully; invalid input preserves clipboard")
''')
            built = subprocess.run(['swiftc','-module-cache-path',str(root/'modules'),str(main),'-o',str(root/'test')],capture_output=True,text=True,timeout=90)
            self.assertEqual(built.returncode,0,built.stdout+built.stderr)
            result = subprocess.run([str(root/'test')],capture_output=True,text=True,timeout=15)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
