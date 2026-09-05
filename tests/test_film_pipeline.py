import unittest
import json

import numpy as np

import film_pipeline as fp


class FilmParamsTests(unittest.TestCase):
    def test_every_selectable_stock_has_a_grain_baseline(self):
        selectable = set(fp.NEGATIVES) | fp.POSITIVE_STOCKS
        self.assertEqual(set(fp.STOCK_GRAIN_AREA_UM2), selectable)

    def test_stock_changes_native_grain_at_the_same_user_amount(self):
        slow = fp.effective_grain_area_um2({
            "stock": "kodak_ektar_100", "grain_amount": 1.0,
        })
        fast = fp.effective_grain_area_um2({
            "stock": "kodak_portra_800", "grain_amount": 1.0,
        })
        pushed = fp.effective_grain_area_um2({
            "stock": "kodak_portra_800_push2", "grain_amount": 1.0,
        })

        self.assertEqual(slow, 0.10)
        self.assertEqual(fast, 0.80)
        self.assertEqual(pushed, 1.60)

    def test_user_grain_amount_scales_the_stock_baseline(self):
        self.assertEqual(fp.effective_grain_area_um2({
            "stock": "kodak_portra_400", "grain_amount": 1.5,
        }), 0.60)

    def test_legacy_absolute_grain_migrates_without_changing_render(self):
        cleaned = fp.clean_params({
            "stock": "kodak_portra_800", "grain_um2": 0.20,
        })

        self.assertEqual(cleaned["grain_amount"], 0.25)
        self.assertNotIn("grain_um2", cleaned)
        self.assertEqual(fp.effective_grain_area_um2(cleaned), 0.20)

    def test_grain_area_uses_engine_field_name_and_effective_value(self):
        params = fp.rust_params_json({
            "stock": "kodak_portra_800", "grain_amount": 1.5,
        })
        grain = params["film_render"]["grain"]

        self.assertEqual(grain["agx_particle_area_um2"], 1.20)
        self.assertNotIn("particle_area_um2", grain)

    def test_development_time_is_sent_only_when_selected(self):
        default = fp.rust_params_json({})["film_render"]
        developed = fp.rust_params_json({
            "stock": "kodak_doublex", "development_time": 9.0,
        })["film_render"]

        self.assertNotIn("development_time", default)
        self.assertEqual(developed["development_time"], 9.0)

    def test_print_development_time_is_routed_separately(self):
        params = fp.rust_params_json({
            "stock": "kodak_doublex",
            "paper": "kodak_2302",
            "development_time": 6.5,
            "print_development_time": 7.0,
        })
        self.assertEqual(params["film_render"]["development_time"], 6.5)
        self.assertEqual(params["print_render"]["development_time"], 7.0)

    def test_glare_controls_the_print_scan_stage_not_unused_film_field(self):
        disabled = fp.rust_params_json({"glare_on": False})
        enabled = fp.rust_params_json({"glare_on": True})
        self.assertFalse(disabled["film_render"]["glare"]["active"])
        self.assertFalse(disabled["print_render"]["glare"]["active"])
        self.assertTrue(enabled["print_render"]["glare"]["active"])
        self.assertEqual(enabled["print_render"]["glare"]["roughness"], 0.0)

    def test_reversal_scan_disables_print_glare(self):
        params = fp.rust_params_json({
            "stock": "fujifilm_velvia_100", "glare_on": True,
        })
        self.assertTrue(params["io"]["scan_film"])
        self.assertFalse(params["print_render"]["glare"]["active"])

    def test_film_format_and_output_recipe_reach_both_engines(self):
        source = {"film_format": "6x7", "output_recipe": "soft_optical_print"}
        rust = fp.rust_params_json(source)
        python = fp.build_params(source)
        self.assertEqual(rust["camera"]["film_format_mm"], 70.0)
        self.assertEqual(python.camera.film_format_mm, 70.0)
        self.assertEqual(rust["scanner"]["lens_blur"], 0.35)
        self.assertEqual(python.scanner.lens_blur, 0.35)

    def test_authentic_mode_follows_target_print_unless_locked(self):
        authentic = fp.clean_params({
            "stock": "fujifilm_c200", "paper": "kodak_portra_endura",
        })
        locked = fp.clean_params({
            "stock": "fujifilm_c200", "paper": "kodak_portra_endura",
            "paper_locked": True,
        })
        creative = fp.clean_params({
            "stock": "fujifilm_c200", "paper": "kodak_portra_endura",
            "workflow_mode": "creative",
        })
        self.assertEqual(authentic["paper"], "fujifilm_crystal_archive_typeii")
        self.assertEqual(locked["paper"], "kodak_portra_endura")
        self.assertEqual(creative["paper"], "kodak_portra_endura")

    def test_invalid_workflow_repairs_to_authentic_before_paper_pairing(self):
        cleaned = fp.clean_params({
            "stock": "fujifilm_c200", "paper": "kodak_portra_endura",
            "workflow_mode": "unknown",
        })
        self.assertEqual(cleaned["workflow_mode"], "authentic")
        self.assertEqual(cleaned["paper"], "fujifilm_crystal_archive_typeii")

    def test_numeric_params_are_finite_and_bounded(self):
        cleaned = fp.clean_params({
            "exposure_ev": float("nan"), "grain_amount": -1,
            "print_y_filter_shift": -100,
        })
        self.assertEqual(cleaned["exposure_ev"], 0.0)
        self.assertEqual(cleaned["grain_amount"], 0.0)
        self.assertEqual(cleaned["print_y_filter_shift"], -20.0)

    def test_python_params_use_the_same_effective_grain_value(self):
        params = fp.build_params({
            "stock": "kodak_portra_800", "grain_amount": 1.5,
        })
        self.assertEqual(params.film_render.grain.particle_area_um2, 1.20)

    def test_halation_keeps_the_engine_stock_baseline(self):
        gold = fp.build_params({"stock": "kodak_gold_200"})
        portra = fp.build_params({"stock": "kodak_portra_400"})
        gold = fp.spektrafilm.digest_params(gold)
        portra = fp.spektrafilm.digest_params(portra)

        self.assertEqual(gold.film_render.halation.halation_strength,
                         (0.08, 0.02, 0.0))
        self.assertEqual(portra.film_render.halation.halation_strength,
                         (0.015, 0.005, 0.0))

    def test_profile_is_on_by_default_and_can_be_disabled(self):
        self.assertTrue(fp.clean_params({})["profile_enabled"])
        self.assertFalse(fp.clean_params({"profile_enabled": False})["profile_enabled"])

    def test_develop_profile_is_a_raw_develop_key_and_survives_cleaning(self):
        # server.clean_raw_develop_settings filters a cleaned dict down to
        # these keys before storing a per-camera default in prefs.json, so
        # the key has to be listed and present after cleaning.
        self.assertIn("developProfile", fp.RAW_DEVELOP_KEYS)
        cleaned = fp.clean_params({})
        self.assertTrue(set(fp.RAW_DEVELOP_KEYS) <= set(cleaned))

        self.assertEqual(cleaned["developProfile"], "standard")
        scanned = fp.clean_params({"developProfile": "linear"})
        self.assertEqual(scanned["developProfile"], "linear")
        self.assertEqual(fp.clean_params(scanned)["developProfile"], "linear")
        self.assertEqual(
            fp.clean_params({"developProfile": "kodachrome"})["developProfile"],
            "standard")

    def test_disabled_profile_bypasses_film_simulation(self):
        image = np.array([[[0.0, 0.25, 1.0]]], dtype=np.float64)
        out = fp.render(image, {"profile_enabled": False,
                                "linear_input": False})
        np.testing.assert_array_equal(out, np.array([[[0, 64, 255]]], dtype=np.uint8))

    def test_float_render_preserves_sub_eight_bit_values(self):
        value = 0.500123
        image = np.full((1, 1, 3), value, dtype=np.float64)
        out = fp.render_float(image, {"profile_enabled": False,
                                      "linear_input": False})
        self.assertEqual(out.dtype, np.float32)
        self.assertAlmostEqual(float(out[0, 0, 0]), value, places=6)
        self.assertNotEqual(float(out[0, 0, 0]), round(value * 255) / 255)


class FilmProfileCatalogTests(unittest.TestCase):
    def test_hidden_profiles_are_exposed_by_stage_metadata(self):
        self.assertIn("kodak_doublex", fp.NEGATIVES)
        self.assertIn("kodak_tmax_p3200", fp.NEGATIVES)
        self.assertIn("kodak_trix", fp.POSITIVES)
        self.assertIn("kodak_2302", fp.PAPERS)
        self.assertIn("kodak_endura_premier", fp.PAPERS)

    def test_every_negative_has_a_compatible_default_print(self):
        for film in fp.FILM_PROFILES:
            if film["type"] != "negative":
                continue
            paper = fp.PROFILE_BY_ID[fp.default_paper(film["id"])]
            self.assertEqual(paper["stage"], "printing", film["id"])
            self.assertEqual(paper["channelModel"], film["channelModel"],
                             film["id"])

    def test_black_and_white_profiles_are_rust_only(self):
        bw = [p for p in fp.PROFILE_CATALOG if p["channelModel"] == "bw"]
        self.assertTrue(bw)
        self.assertTrue(all(p["rustOnly"] for p in bw))

    def test_trix_has_calibrated_direct_scan_exposure(self):
        self.assertEqual(fp.PROFILE_BY_ID["kodak_trix"]["defaultExposureEv"],
                         -1.5)

    def test_incompatible_paper_is_repaired_for_bw_negative(self):
        params = fp.clean_params({
            "stock": "kodak_tmax_p3200",
            "paper": "kodak_portra_endura",
        })
        self.assertEqual(params["paper"], "kodak_2302")

    def test_p3200_profile_preserves_published_setup_and_curve_shape(self):
        path = fp.CUSTOM_PROFILE_DIR / "kodak_tmax_p3200.json"
        profile = json.loads(path.read_text())
        density = np.asarray(profile["data"]["density_curves"])[:, 0]

        self.assertEqual(profile["data"]["development_time"], [12.0])
        self.assertEqual(profile["info"]["target_print"], "kodak_2302")
        self.assertEqual(
            profile["metadata"]["citation"],
            "https://business.kodakmoments.com/sites/default/files/files/"
            "products/F4001.pdf",
        )
        self.assertIn("March 2018", profile["metadata"]["datasource"])
        self.assertNotIn("midpoint", profile["metadata"]["datasource"])
        self.assertAlmostEqual(float(density.min()), 0.0, places=6)
        self.assertAlmostEqual(float(density.max()), 2.2, places=6)
        self.assertTrue(np.all(np.diff(density) >= 0))

    def test_tracked_p3200_profile_matches_its_generator(self):
        from profiles.build_kodak_tmax_p3200 import build_profile

        tracked = json.loads((fp.CUSTOM_PROFILE_DIR /
                              "kodak_tmax_p3200.json").read_text())
        self.assertEqual(tracked, build_profile())


if __name__ == "__main__":
    unittest.main()
