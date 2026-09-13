# SPDX-License-Identifier: GPL-3.0-only
import base64
import io
import json
import unittest
import zipfile

import preset_io


LIGHTROOM_XMP = """<?xpacket begin=''?>
<x:xmpmeta xmlns:x='adobe:ns:meta/'>
 <rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>
  <rdf:Description xmlns:crs='http://ns.adobe.com/camera-raw-settings/1.0/'
   crs:Exposure2012="+0.75" crs:Contrast2012="20"
   crs:IncrementalTemperature="-20" crs:IncrementalTint="15"
   crs:Texture="35" crs:Sharpness="60" crs:LuminanceSmoothing="25"
   crs:HueAdjustmentBlue="-20" crs:CameraProfile="Adobe Color"
   crs:UUID="fixture" crs:SupportsColor="True">
   <crs:Name><rdf:Alt><rdf:li xml:lang='x-default'>Cool Street</rdf:li></rdf:Alt></crs:Name>
   <crs:ToneCurvePV2012><rdf:Seq>
    <rdf:li>0, 0</rdf:li><rdf:li>128, 142</rdf:li><rdf:li>255, 255</rdf:li>
   </rdf:Seq></crs:ToneCurvePV2012>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>"""

LIGHTROOM_TEMPLATE = '''s = {
 id = "B27A",
 internalName = "Legacy Matte",
 title = "Legacy Matte",
 type = "Develop",
 value = {
  settings = {
   Exposure2012 = 0.4,
   Contrast2012 = -15,
   Sharpness = 45,
   ToneCurvePV2012 = {
    0, 0,
    128, 136,
    255, 250,
   },
  },
 },
}'''

CAPTURE_ONE_STYLE = """<?xml version="1.0"?>
<SL Engine="1600">
 <E K="Exposure" V="0.5" />
 <E K="Contrast" V="18" />
 <E K="Structure" V="30" />
 <E K="UsmAmount" V="150" />
 <E K="UsmRadius" V="0.8" />
 <E K="FilmCurve" V="Some proprietary curve.fcrv" />
 <E K="Name" V="Studio Neutral" />
</SL>"""


class LightroomImportTests(unittest.TestCase):
    def test_maps_core_detail_hsl_and_curve_with_conversion_report(self):
        preset = preset_io.import_lightroom(LIGHTROOM_XMP, "fallback.xmp")
        self.assertEqual(preset["name"], "Cool Street")
        self.assertEqual(preset["source"], "lightroom")
        self.assertEqual(preset["grade"]["exposure"], 0.75)
        self.assertEqual(preset["grade"]["contrast"], 0.2)
        self.assertEqual(preset["grade"]["texture"], 0.35)
        self.assertEqual(preset["grade"]["temp"], -0.2)
        self.assertEqual(preset["grade"]["tint"], 0.15)
        self.assertEqual(preset["grade"]["sharpness"], 0.4)
        self.assertEqual(preset["grade"]["luminanceNoise"], 0.25)
        self.assertEqual(preset["grade"]["hsl"]["blue"]["h"], -0.2)
        self.assertEqual(len(preset["grade"]["curveL"]), 256)
        self.assertIn("embedded profile or look", preset["conversion"]["ignored"])
        self.assertNotIn("UUID", preset["conversion"]["ignored"])
        self.assertTrue(preset["recommendedFilmOff"])

    def test_nonnegative_detail_controls_do_not_import_below_zero(self):
        preset = preset_io.import_lightroom(
            "<crs:Description crs:LuminanceSmoothing='-30' "
            "crs:SharpenEdgeMasking='-10' />",
            "defensive.xmp",
        )
        self.assertEqual(preset["grade"]["luminanceNoise"], 0)
        self.assertEqual(preset["grade"]["sharpenMasking"], 0)

    def test_legacy_lua_template_imports_without_executing_source(self):
        preset = preset_io.import_lightroom(LIGHTROOM_TEMPLATE, "legacy.lrtemplate")
        self.assertEqual(preset["name"], "Legacy Matte")
        self.assertEqual(preset["grade"]["exposure"], 0.4)
        self.assertEqual(preset["grade"]["contrast"], -0.15)
        self.assertEqual(preset["grade"]["sharpness"], 0.3)
        self.assertEqual(len(preset["grade"]["curveL"]), 256)

    def test_profile_only_xmp_is_reported_without_metadata_noise(self):
        preset = preset_io.import_lightroom(
            '<rdf:Description crs:PresetType="Look" crs:UUID="fixture" '
            'crs:SupportsColor="True" crs:ConvertToGrayscale="False" '
            'crs:RGBTable="TABLE-ID" crs:Table_TABLEID="payload" />',
            "profile.xmp",
        )
        self.assertEqual(preset["conversion"]["mapped"], 0)
        self.assertEqual(preset["conversion"]["ignored"],
                         ["embedded profile or look"])

    def test_black_and_white_conversion_maps_to_monochrome_and_the_mixer(self):
        preset = preset_io.import_lightroom(
            '<rdf:Description crs:ConvertToGrayscale="True" '
            'crs:GrayMixerRed="-30" crs:GrayMixerBlue="45" crs:GrayMixerGreen="0" '
            'crs:HueAdjustmentBlue="10" crs:LuminanceAdjustmentBlue="-80" '
            'crs:LuminanceAdjustmentYellow="20" />',
            "silver.xmp",
        )
        grade = preset["grade"]
        self.assertEqual(grade["monochrome"], 1.0)
        self.assertEqual(grade["hsl"]["red"], {"l": -0.3})
        # The grey mixer replaces the colour luminance panel, which is inert in B&W.
        self.assertEqual(grade["hsl"]["blue"], {"h": 0.1, "l": 0.45})
        self.assertNotIn("yellow", grade["hsl"])
        self.assertNotIn("green", grade["hsl"])
        self.assertIn("monochrome", preset["includedGrade"])
        self.assertEqual(preset["conversion"]["ignored"], [])
        self.assertEqual(preset["conversion"]["mapped"], 2)

    def test_grey_mixer_values_are_inert_while_colour_treatment_is_on(self):
        preset = preset_io.import_lightroom(
            '<rdf:Description crs:ConvertToGrayscale="False" '
            'crs:GrayMixerRed="-30" crs:LuminanceAdjustmentRed="25" />',
            "colour.xmp",
        )
        self.assertNotIn("monochrome", preset["grade"])
        self.assertEqual(preset["grade"]["hsl"]["red"], {"l": 0.25})
        self.assertEqual(preset["conversion"]["ignored"], [])

    def test_exposure_uses_the_apps_range_and_reports_clamping(self):
        from grade import RANGES
        low, high = RANGES["exposure"]
        preset = preset_io.import_lightroom(
            '<rdf:Description crs:Exposure2012="+4.50" />', "bright.xmp")
        self.assertEqual(preset["grade"]["exposure"], 4.5)
        self.assertEqual(preset["conversion"]["ignored"], [])
        preset = preset_io.import_lightroom(
            '<rdf:Description crs:Exposure2012="+7.25" />', "blown.xmp")
        self.assertEqual(preset["grade"]["exposure"], high)
        self.assertEqual(preset["conversion"]["ignored"],
                         [f"exposure beyond {low:+g} to {high:+g} clamped to range"])
        self.assertTrue(any("+7.25" in note for note in preset["conversion"]["notes"]))
        converted = preset_io.map_crs_settings({"Exposure2012": "-9"})
        self.assertEqual(converted["grade"]["exposure"], low)
        self.assertEqual(converted["clamped"], [{
            "control": "exposure", "source": -9.0, "value": low, "min": low, "max": high}])

    def test_old_process_version_exposure_and_curve_import(self):
        preset = preset_io.import_lightroom(
            '<rdf:Description crs:Exposure="-0.4">'
            '<crs:ToneCurve><rdf:Seq><rdf:li>0, 8</rdf:li>'
            '<rdf:li>255, 245</rdf:li></rdf:Seq></crs:ToneCurve>'
            '</rdf:Description>',
            "pv2.xmp",
        )
        self.assertEqual(preset["grade"]["exposure"], -0.4)
        self.assertEqual(len(preset["grade"]["curveL"]), 256)


class CaptureOneImportTests(unittest.TestCase):
    def test_imports_single_style_and_reports_engine_specific_values(self):
        preset = preset_io.import_capture_one(CAPTURE_ONE_STYLE, "fallback.costyle")
        self.assertEqual(preset["name"], "Studio Neutral")
        self.assertEqual(preset["grade"]["exposure"], 0.5)
        self.assertEqual(preset["grade"]["contrast"], 0.18)
        self.assertEqual(preset["grade"]["texture"], 0.3)
        self.assertEqual(preset["grade"]["sharpness"], 0.5)
        self.assertIn("FilmCurve", preset["conversion"]["ignored"])

    def test_style_exposure_beyond_the_app_range_is_reported(self):
        from grade import RANGES
        preset = preset_io.import_capture_one(
            '<SL><E K="Exposure" V="-6.5" /><E K="Name" V="Deep" /></SL>', "deep.costyle")
        self.assertEqual(preset["grade"]["exposure"], RANGES["exposure"][0])
        self.assertTrue(any(item.startswith("exposure beyond")
                            for item in preset["conversion"]["ignored"]))

    def test_imports_costylepack_archive(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("Portrait/Studio.costyle", CAPTURE_ONE_STYLE)
        presets, failures = preset_io.import_uploads([{
            "name": "Portrait.costylepack",
            "base64": base64.b64encode(archive.getvalue()).decode(),
        }])
        self.assertFalse(failures)
        self.assertEqual([preset["name"] for preset in presets], ["Studio Neutral"])


class PresetExportTests(unittest.TestCase):
    def setUp(self):
        self.preset = {
            "name": "Portable Look", "presetType": "style",
            "grade": {"exposure": 0.5, "contrast": 0.2,
                      "sharpness": 0.4, "sharpenRadius": 0.8},
            "includedGrade": ["exposure", "contrast", "sharpness", "sharpenRadius"],
        }

    def test_lighttable_export_round_trip_preserves_native_fields(self):
        native = dict(self.preset, includeFilm=True,
                      grade={**self.preset["grade"], "vignette": .6,
                             "vignetteSize": .2, "vignetteFeather": .4},
                      includedGrade=[*self.preset["includedGrade"], "vignette",
                                     "vignetteSize", "vignetteFeather"],
                      params={"profile_enabled": True, "stock": "test-stock"},
                      masks=[{"type": "radial", "grade": {"exposure": 1}}],
                      heals=[{"mode": "remove", "target": [0.5, 0.5]}],
                      optics={"profileEnabled": True, "vertical": 0.2})
        filename, _, content = preset_io.export_preset(native, "lighttable")
        imported = preset_io._import_bytes(filename, content.encode())[0]
        self.assertTrue(filename.endswith(".ltpreset"))
        self.assertEqual(imported["grade"], native["grade"])
        self.assertEqual(imported["includedGrade"], native["includedGrade"])
        self.assertEqual(imported["params"], native["params"])
        self.assertEqual(imported["masks"], native["masks"])
        self.assertEqual(imported["heals"], native["heals"])
        self.assertEqual(imported["optics"], native["optics"])
        self.assertEqual(json.loads(content)["version"], 2)

    def test_lightroom_and_capture_one_exports_identify_their_formats(self):
        xmp_name, _, xmp = preset_io.export_lightroom(self.preset)
        style_name, _, style = preset_io.export_capture_one(self.preset)
        self.assertTrue(xmp_name.endswith(".xmp"))
        self.assertIn("crs:Exposure2012", xmp)
        self.assertNotIn("crs:ConvertToGrayscale", xmp)
        mono = dict(self.preset, grade={**self.preset["grade"], "monochrome": 1,
                                        "hsl": {"red": {"l": -0.3}}},
                    includedGrade=[*self.preset["includedGrade"], "monochrome", "hsl"])
        _, _, mono_xmp = preset_io.export_lightroom(mono)
        self.assertIn('crs:ConvertToGrayscale="True"', mono_xmp)
        self.assertIn('crs:GrayMixerRed="-30.0"', mono_xmp)
        round_trip = preset_io.import_lightroom(mono_xmp, "mono.xmp")
        self.assertEqual(round_trip["grade"]["monochrome"], 1.0)
        self.assertEqual(round_trip["grade"]["hsl"]["red"]["l"], -0.3)
        self.assertTrue(style_name.endswith(".costyle"))
        self.assertIn('K="Exposure"', style)


if __name__ == "__main__":
    unittest.main()
