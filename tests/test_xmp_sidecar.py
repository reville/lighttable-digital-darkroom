import tempfile
import unittest
from pathlib import Path

import preset_io
import xmp_sidecar


ATTRIBUTE_XMP = """<?xpacket begin='' id='W5M0MpCehiHzreSzNTczkc9d'?>
<x:xmpmeta xmlns:x='adobe:ns:meta/'>
 <rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>
  <rdf:Description rdf:about=''
   xmlns:xmp='http://ns.adobe.com/xap/1.0/'
   xmlns:dc='http://purl.org/dc/elements/1.1/'
   xmlns:crs='http://ns.adobe.com/camera-raw-settings/1.0/'
   xmlns:lr='http://ns.adobe.com/lightroom/1.0/'
   xmlns:exif='http://ns.adobe.com/exif/1.0/'
   xmlns:tiff='http://ns.adobe.com/tiff/1.0/'
   xmlns:photoshop='http://ns.adobe.com/photoshop/1.0/'
   xmp:Rating="4" xmp:Label="Yellow" tiff:Orientation="6"
   exif:GPSLatitude="51,30.5N" exif:GPSLongitude="0,7.4W"
   exif:GPSAltitude="42/1"
   photoshop:Headline="Winter hedge" photoshop:Credit="Film Lab"
   photoshop:City="London" photoshop:State="Greater London"
   photoshop:Country="United Kingdom"
   crs:Exposure2012="+0.75" crs:Contrast2012="20" crs:Texture="35"
   crs:IncrementalTemperature="-20" crs:CameraProfile="Adobe Color"
   crs:HasCrop="True" crs:CropLeft="0.1" crs:CropTop="0.2"
   crs:CropRight="0.9" crs:CropBottom="0.8" crs:CropAngle="-2.5">
   <dc:title><rdf:Alt><rdf:li xml:lang='x-default'>Hedge row</rdf:li></rdf:Alt></dc:title>
   <dc:description><rdf:Alt><rdf:li xml:lang='x-default'>Frost on the lane.</rdf:li></rdf:Alt></dc:description>
   <dc:creator><rdf:Seq><rdf:li>N Reville</rdf:li></rdf:Seq></dc:creator>
   <dc:rights><rdf:Alt><rdf:li xml:lang='x-default'>(c) 2026 N Reville</rdf:li></rdf:Alt></dc:rights>
   <dc:subject><rdf:Bag><rdf:li>oak</rdf:li><rdf:li>winter</rdf:li></rdf:Bag></dc:subject>
   <lr:hierarchicalSubject><rdf:Bag>
    <rdf:li>nature|trees|oak</rdf:li><rdf:li>season|winter</rdf:li>
   </rdf:Bag></lr:hierarchicalSubject>
   <crs:ToneCurvePV2012><rdf:Seq>
    <rdf:li>0, 0</rdf:li><rdf:li>128, 142</rdf:li><rdf:li>255, 255</rdf:li>
   </rdf:Seq></crs:ToneCurvePV2012>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end='w'?>"""

# The same photo written in element form, with deliberately different
# namespace prefixes: only the namespace URI is meaningful.
ELEMENT_XMP = """<x:xmpmeta xmlns:x='adobe:ns:meta/'>
 <rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>
  <rdf:Description rdf:about=''
   xmlns:a='http://ns.adobe.com/xap/1.0/'
   xmlns:b='http://purl.org/dc/elements/1.1/'
   xmlns:c='http://ns.adobe.com/camera-raw-settings/1.0/'
   xmlns:d='http://ns.adobe.com/lightroom/1.0/'
   xmlns:e='http://ns.adobe.com/exif/1.0/'
   xmlns:f='http://ns.adobe.com/tiff/1.0/'
   xmlns:g='http://ns.adobe.com/photoshop/1.0/'>
   <a:Rating>4</a:Rating>
   <a:Label>Yellow</a:Label>
   <f:Orientation>6</f:Orientation>
   <e:GPSLatitude>51,30.5N</e:GPSLatitude>
   <e:GPSLongitude>0,7.4W</e:GPSLongitude>
   <e:GPSAltitude>42/1</e:GPSAltitude>
   <g:Headline>Winter hedge</g:Headline>
   <g:Credit>Film Lab</g:Credit>
   <g:City>London</g:City>
   <g:State>Greater London</g:State>
   <g:Country>United Kingdom</g:Country>
   <c:Exposure2012>+0.75</c:Exposure2012>
   <c:Contrast2012>20</c:Contrast2012>
   <c:Texture>35</c:Texture>
   <c:IncrementalTemperature>-20</c:IncrementalTemperature>
   <c:CameraProfile>Adobe Color</c:CameraProfile>
   <c:HasCrop>True</c:HasCrop>
   <c:CropLeft>0.1</c:CropLeft><c:CropTop>0.2</c:CropTop>
   <c:CropRight>0.9</c:CropRight><c:CropBottom>0.8</c:CropBottom>
   <c:CropAngle>-2.5</c:CropAngle>
   <b:title><rdf:Alt><rdf:li xml:lang='x-default'>Hedge row</rdf:li></rdf:Alt></b:title>
   <b:description><rdf:Alt><rdf:li xml:lang='x-default'>Frost on the lane.</rdf:li></rdf:Alt></b:description>
   <b:creator><rdf:Seq><rdf:li>N Reville</rdf:li></rdf:Seq></b:creator>
   <b:rights><rdf:Alt><rdf:li xml:lang='x-default'>(c) 2026 N Reville</rdf:li></rdf:Alt></b:rights>
   <b:subject><rdf:Bag><rdf:li>oak</rdf:li><rdf:li>winter</rdf:li></rdf:Bag></b:subject>
   <d:hierarchicalSubject><rdf:Bag>
    <rdf:li>nature|trees|oak</rdf:li><rdf:li>season|winter</rdf:li>
   </rdf:Bag></d:hierarchicalSubject>
   <c:ToneCurvePV2012><rdf:Seq>
    <rdf:li>0, 0</rdf:li><rdf:li>128, 142</rdf:li><rdf:li>255, 255</rdf:li>
   </rdf:Seq></c:ToneCurvePV2012>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>"""

REJECTED_XMP = """<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>
 <rdf:Description rdf:about=''
  xmlns:xmp='http://ns.adobe.com/xap/1.0/' xmp:Rating="-1" xmp:Label="To Print" />
</rdf:RDF>"""

DECIMAL_GPS_XMP = """<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>
 <rdf:Description rdf:about='' xmlns:exif='http://ns.adobe.com/exif/1.0/'
  exif:GPSLatitude="-33.8688" exif:GPSLongitude="151.2093"
  exif:GPSAltitude="8/1" exif:GPSAltitudeRef="1" />
</rdf:RDF>"""

FULL_FRAME_XMP = """<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>
 <rdf:Description rdf:about=''
  xmlns:crs='http://ns.adobe.com/camera-raw-settings/1.0/'
  crs:HasCrop="True" crs:CropLeft="0" crs:CropTop="0"
  crs:CropRight="1" crs:CropBottom="1" />
</rdf:RDF>"""

STEEP_ANGLE_XMP = """<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>
 <rdf:Description rdf:about=''
  xmlns:crs='http://ns.adobe.com/camera-raw-settings/1.0/'
  crs:Exposure2012="0.25" crs:HasCrop="True" crs:CropLeft="0.05"
  crs:CropTop="0.05" crs:CropRight="0.95" crs:CropBottom="0.95"
  crs:CropAngle="22.5" />
</rdf:RDF>"""

MALFORMED_XMP = """<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>
 <rdf:Description crs:Exposure2012="0.5">
</rdf:RDF>"""


class ParseTests(unittest.TestCase):
    def test_attribute_and_element_forms_read_the_same_photo(self):
        attribute = xmp_sidecar.parse(ATTRIBUTE_XMP)
        element = xmp_sidecar.parse(ELEMENT_XMP)
        for parsed in (attribute, element):
            self.assertEqual(parsed["rating"], 4)
            self.assertEqual(parsed["label"], "yellow")
            self.assertEqual(parsed["orientation"], 6)
            self.assertEqual(parsed["title"], "Hedge row")
            self.assertEqual(parsed["caption"], "Frost on the lane.")
            self.assertEqual(parsed["creator"], "N Reville")
            self.assertEqual(parsed["copyright"], "(c) 2026 N Reville")
            self.assertEqual(parsed["keywords"], ["oak", "winter"])
            self.assertEqual(parsed["crs"]["Exposure2012"], "+0.75")
            self.assertEqual(parsed["crs"]["CameraProfile"], "Adobe Color")
            self.assertEqual(len(parsed["crsCurves"]["ToneCurvePV2012"]), 3)
        for field in ("rating", "label", "keywords", "keywordPaths", "title",
                      "caption", "creator", "copyright", "gps", "orientation",
                      "crop", "cropAngle", "crs", "headline", "credit",
                      "city", "state", "country"):
            self.assertEqual(attribute[field], element[field], field)

    def test_iptc_location_and_credit_fields_are_named(self):
        parsed = xmp_sidecar.parse(ATTRIBUTE_XMP)
        self.assertEqual(parsed["headline"], "Winter hedge")
        self.assertEqual(parsed["credit"], "Film Lab")
        self.assertEqual(parsed["city"], "London")
        self.assertEqual(parsed["state"], "Greater London")
        self.assertEqual(parsed["country"], "United Kingdom")

    def test_hierarchical_keywords_keep_their_parent_paths(self):
        parsed = xmp_sidecar.parse(ATTRIBUTE_XMP)
        self.assertEqual(parsed["keywordPaths"],
                         ["nature|trees|oak", "season|winter"])
        self.assertEqual(parsed["keywords"], ["oak", "winter"])

    def test_rejected_rating_is_dropped_and_custom_label_is_kept(self):
        parsed = xmp_sidecar.parse(REJECTED_XMP)
        self.assertIsNone(parsed["rating"])
        self.assertTrue(parsed["rejected"])
        self.assertEqual(parsed["label"], "To Print")
        self.assertFalse(xmp_sidecar.parse(ATTRIBUTE_XMP)["rejected"])

    def test_gps_reads_both_the_reference_and_decimal_forms(self):
        reference = xmp_sidecar.parse(ATTRIBUTE_XMP)["gps"]
        self.assertAlmostEqual(reference["lat"], 51.508333, places=5)
        self.assertAlmostEqual(reference["lon"], -0.123333, places=5)
        self.assertEqual(reference["alt"], 42.0)
        decimal = xmp_sidecar.parse(DECIMAL_GPS_XMP)["gps"]
        self.assertAlmostEqual(decimal["lat"], -33.8688, places=5)
        self.assertAlmostEqual(decimal["lon"], 151.2093, places=5)
        self.assertEqual(decimal["alt"], -8.0)

    def test_missing_coordinates_leave_gps_unset(self):
        self.assertIsNone(xmp_sidecar.parse(REJECTED_XMP)["gps"])

    def test_malformed_and_oversized_documents_return_none(self):
        self.assertIsNone(xmp_sidecar.parse(MALFORMED_XMP))
        self.assertIsNone(xmp_sidecar.parse(""))
        self.assertIsNone(xmp_sidecar.parse("not xml at all"))
        self.assertIsNone(
            xmp_sidecar.parse("<a/>" + " " * (xmp_sidecar.MAX_XMP_BYTES + 1)))


class CropTests(unittest.TestCase):
    def test_crop_fractions_become_the_apps_normalised_rect(self):
        parsed = xmp_sidecar.parse(ATTRIBUTE_XMP)
        self.assertEqual(parsed["crop"],
                         {"x": 0.1, "y": 0.2, "w": 0.8, "h": 0.6})
        self.assertEqual(parsed["cropAngle"], -2.5)
        self.assertEqual(xmp_sidecar.as_edit_patch(parsed)["crop"],
                         parsed["crop"])

    def test_full_frame_and_disabled_crops_stay_empty(self):
        self.assertIsNone(xmp_sidecar.parse(FULL_FRAME_XMP)["crop"])
        disabled = FULL_FRAME_XMP.replace('crs:HasCrop="True"',
                                          'crs:HasCrop="False"')
        self.assertIsNone(xmp_sidecar.parse(disabled)["crop"])

    def test_supported_straighten_angle_becomes_optics_rotate(self):
        patch = xmp_sidecar.as_edit_patch(xmp_sidecar.parse(ATTRIBUTE_XMP))
        self.assertEqual(patch["optics"]["rotate"], -2.5)
        self.assertNotIn(xmp_sidecar.CROP_ANGLE_SKIPPED, patch["ignored"])

    def test_steep_straighten_angle_is_reported_as_skipped(self):
        patch = xmp_sidecar.as_edit_patch(xmp_sidecar.parse(STEEP_ANGLE_XMP))
        self.assertEqual(patch["optics"], {})
        self.assertIn(xmp_sidecar.CROP_ANGLE_SKIPPED, patch["ignored"])
        self.assertEqual(patch["crop"]["w"], 0.9)


class SidecarFileTests(unittest.TestCase):
    def test_both_naming_forms_are_found_on_disk(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            replaced = root / "a.xmp"
            replaced.write_text(ATTRIBUTE_XMP)
            appended = root / "b.CR2.xmp"
            appended.write_text(ATTRIBUTE_XMP)
            self.assertEqual(xmp_sidecar.find_sidecar(root / "a.CR2"), replaced)
            self.assertEqual(xmp_sidecar.find_sidecar(root / "b.CR2"), appended)
            self.assertIsNone(xmp_sidecar.find_sidecar(root / "c.CR2"))
            self.assertEqual(
                [path.name for path in xmp_sidecar.sidecar_paths(root / "a.CR2")],
                ["a.xmp", "a.CR2.xmp"])

    def test_reading_a_sidecar_returns_the_parsed_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "frame.RAF.xmp").write_text(ATTRIBUTE_XMP)
            parsed = xmp_sidecar.read_sidecar(root / "frame.RAF")
            self.assertEqual(parsed["rating"], 4)
            self.assertEqual(parsed["origin"], "sidecar")
            self.assertEqual(xmp_sidecar.read_for(root / "frame.RAF")["title"],
                             "Hedge row")
            self.assertIsNone(xmp_sidecar.read_sidecar(root / "missing.RAF"))
            self.assertIsNone(xmp_sidecar.read_for(root / "missing.RAF"))

    def test_the_adobe_name_wins_when_both_sidecars_exist(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "frame.xmp").write_text(ATTRIBUTE_XMP)
            (root / "frame.RAF.xmp").write_text(REJECTED_XMP)
            self.assertEqual(xmp_sidecar.find_sidecar(root / "frame.RAF"),
                             root / "frame.xmp")
            self.assertEqual(
                xmp_sidecar.read_sidecar(root / "frame.RAF")["rating"], 4)

    def test_an_oversized_sidecar_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "huge.xmp"
            with path.open("wb") as handle:
                handle.truncate(xmp_sidecar.MAX_XMP_BYTES + 1)
            self.assertIsNone(xmp_sidecar.read_sidecar(root / "huge.RAF"))

    def test_embedded_reading_ignores_formats_without_a_packet(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertIsNone(
                xmp_sidecar.read_embedded(Path(folder) / "frame.RAF"))
            self.assertIsNone(
                xmp_sidecar.read_embedded(Path(folder) / "absent.jpg"))


class EditPatchTests(unittest.TestCase):
    def test_grade_matches_the_lightroom_preset_importer(self):
        patch = xmp_sidecar.as_edit_patch(xmp_sidecar.parse(ATTRIBUTE_XMP))
        preset = preset_io.import_lightroom(ATTRIBUTE_XMP, "frame.xmp")
        self.assertEqual(patch["grade"], preset["grade"])
        self.assertEqual(patch["grade"]["exposure"], 0.75)
        self.assertEqual(patch["grade"]["contrast"], 0.2)
        self.assertEqual(patch["grade"]["temp"], -0.2)
        self.assertEqual(len(patch["grade"]["curveL"]), 256)

    def test_element_form_develop_settings_map_the_same_way(self):
        attribute = xmp_sidecar.as_edit_patch(xmp_sidecar.parse(ATTRIBUTE_XMP))
        element = xmp_sidecar.as_edit_patch(xmp_sidecar.parse(ELEMENT_XMP))
        self.assertEqual(attribute["grade"], element["grade"])
        self.assertEqual(attribute["crop"], element["crop"])
        self.assertEqual(attribute["optics"], element["optics"])

    def test_unsupported_develop_settings_are_reported_not_applied(self):
        patch = xmp_sidecar.as_edit_patch(xmp_sidecar.parse(ATTRIBUTE_XMP))
        self.assertIn("embedded profile or look", patch["ignored"])
        self.assertEqual(patch["mapped"], len(patch["grade"]) + 2)

    def test_crop_is_applied_rather_than_reported_as_skipped(self):
        patch = xmp_sidecar.as_edit_patch(xmp_sidecar.parse(ATTRIBUTE_XMP))
        self.assertNotIn("geometry or crop", patch["ignored"])
        self.assertIsNotNone(patch["crop"])

    def test_empty_metadata_produces_an_empty_patch(self):
        patch = xmp_sidecar.as_edit_patch({})
        self.assertEqual(patch["grade"], {})
        self.assertIsNone(patch["crop"])
        self.assertEqual(patch["optics"], {})
        self.assertEqual(patch["mapped"], 0)


if __name__ == "__main__":
    unittest.main()
