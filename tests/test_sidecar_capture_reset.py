"""Exercise capture-time correction/reset through the real persistent outbox."""
import server
import xmp_sidecar
from test_server_catalog import CatalogServerTestCase


class SidecarCaptureResetTests(CatalogServerTestCase):
    def test_reset_restores_foreign_capture_time_through_outbox(self):
        name = self.qualified('a.jpg')
        image_id = server.catalog_image_id(name)
        sidecar = self.root/'a.xmp'
        sidecar.write_text('''<x:xmpmeta xmlns:x="adobe:ns:meta/">
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
<rdf:Description rdf:about="" xmlns:exif="http://ns.adobe.com/exif/1.0/"
exif:DateTimeOriginal="2025-01-02T03:04:05Z"/>
</rdf:RDF></x:xmpmeta>''')
        self.catalog.set_capture_override(image_id, '2026-06-01T12:00:00Z')
        server.queue_sidecar(name)
        self.assertEqual(server.write_pending_sidecars(), 1)
        self.assertEqual(xmp_sidecar.read_sidecar(self.root/'a.jpg')['captureTime'], '2026-06-01T12:00:00+00:00')
        self.catalog.set_capture_override(image_id, None)
        server.queue_sidecar(name)
        self.assertEqual(server.write_pending_sidecars(), 1)
        self.assertEqual(xmp_sidecar.read_sidecar(self.root/'a.jpg')['captureTime'], '2025-01-02T03:04:05Z')
        self.assertEqual(server.sidecar_sync_status()['pending'], 0)
