from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import server
from events import EventBroker, encode_sse
from jobs import JobRegistry
from lighttable_cli.__main__ import (
    OPTICS_DEFAULTS,
    build_parser,
    curve_assignments,
    dispatch,
    dispatch_domain,
    normalize_global_arguments,
    preset_state,
    state_merge_update,
)
from lighttable_cli.instances import discover, select
from lighttable_cli.manifest import INTERNAL_ROUTES, ROUTE_COVERAGE, schema
from validation import ValidationError, clean_state_patch


ROOT = Path(__file__).resolve().parents[1]


class EventBrokerTests(unittest.TestCase):
    def test_slow_subscriber_gets_one_resync_marker(self):
        broker = EventBroker(queue_size=1)
        subscriber = broker.subscribe("window")
        broker.publish("state", {"value": 1})
        broker.publish("state", {"value": 2})
        record = broker.get(subscriber, timeout=0)
        self.assertEqual(record["type"], "resync")
        self.assertEqual(record["id"], 2)

    def test_subscribers_are_bounded_and_sse_is_single_line_json(self):
        broker = EventBroker(maximum_subscribers=1)
        broker.subscribe("window")
        with self.assertRaisesRegex(RuntimeError, "too many"):
            broker.subscribe("second")
        encoded = encode_sse({"id": 3, "type": "state\nevil", "x": "a\nb"})
        self.assertIn(b"event: stateevil\n", encoded)
        self.assertEqual(encoded.count(b"data: "), 1)


class JobRegistryTests(unittest.TestCase):
    def test_job_lifecycle_and_cancel_callback(self):
        called = []
        jobs = JobRegistry(maximum=2)
        first = jobs.create("export", total=4, state="running",
                            cancel=lambda: called.append(True))
        updated = jobs.update(first["id"], progress=2, log=["halfway"])
        self.assertEqual((updated["progress"], updated["state"]), (2, "running"))
        cancelled = jobs.cancel(first["id"])
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertEqual(called, [True])
        self.assertIsNotNone(cancelled["finished"])

    def test_registry_prunes_old_records(self):
        jobs = JobRegistry(maximum=2)
        old = jobs.create("one", state="done")
        jobs.create("two", state="done")
        jobs.create("three", state="done")
        self.assertIsNone(jobs.get(old["id"]))
        self.assertEqual(len(jobs.list()), 2)


class ValidationTests(unittest.TestCase):
    def clean(self, raw, strict=True):
        identity = lambda value: value
        return clean_state_patch(
            raw,
            params_cleaner=lambda value: {"amount": max(0, min(1, float(
                value.get("amount", 0))))},
            grade_cleaner=identity, crop_cleaner=identity,
            masks_cleaner=identity, heals_cleaner=identity,
            optics_cleaner=identity, keywords_cleaner=identity,
            versions_cleaner=identity, label_cleaner=lambda value: str(value),
            params_keys={"amount"}, grade_keys={"exposure"},
            status_values={"pending", "approved", "skipped"}, strict=strict,
        )

    def test_strict_validation_rejects_unknown_and_clamped_values(self):
        with self.assertRaises(ValidationError) as caught:
            self.clean({"params": {"amount": 9, "mystery": 1}})
        self.assertEqual(
            {(item["path"], item["kind"]) for item in caught.exception.issues},
            {("params.amount", "clamped"), ("params.mystery", "unknown")},
        )

    def test_lenient_validation_returns_clean_value_and_warnings(self):
        cleaned, warnings = self.clean({"rating": 8}, strict=False)
        self.assertEqual(cleaned["rating"], 5)
        self.assertEqual(warnings[0]["path"], "rating")


class RequestSecurityTests(unittest.TestCase):
    def handler(self, headers=None):
        handler = server.Handler.__new__(server.Handler)
        handler.server = SimpleNamespace(server_address=("127.0.0.1", 8321))
        handler.headers = headers or {}
        return handler

    def test_host_origin_and_token_are_enforced(self):
        with self.assertRaisesRegex(server.APIError, "Host"):
            self.handler({"Host": "attacker.invalid"})._enforce_security(
                mutating=False)
        with self.assertRaisesRegex(server.APIError, "token"):
            self.handler({"Host": "127.0.0.1:8321"})._enforce_security(
                mutating=True)
        valid = self.handler({"Host": "localhost:8321",
                              "X-LightTable-Token": server.INSTANCE_TOKEN,
                              "Origin": "http://localhost:8321"})
        valid._enforce_security(mutating=True)

    def test_post_body_must_be_json_object(self):
        handler = self.handler({"Content-Type": "text/plain",
                                "Content-Length": "2"})
        handler.rfile = io.BytesIO(b"{}")
        with self.assertRaisesRegex(server.APIError, "application/json"):
            handler._body()

    def test_health_response_never_contains_token(self):
        self.assertNotIn("token", server.health_payload())
        self.assertEqual(server.health_payload(include_token=True)["token"],
                         server.INSTANCE_TOKEN)


class InstanceTests(unittest.TestCase):
    def test_discovery_removes_invalid_and_dead_records(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "8321.json").write_text("not json", encoding="utf-8")
            (root / "9.json").write_text(json.dumps({
                "port": 9, "pid": 99999999, "token": "x"}), encoding="utf-8")
            with mock.patch("lighttable_cli.instances.process_is_alive",
                            return_value=False):
                self.assertEqual(discover(root), [])
            self.assertEqual(list(root.iterdir()), [])

    def test_startup_reports_are_not_instances_or_stale_cleanup_targets(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            registration = root / "8947.json"
            registration.write_text(json.dumps({
                "port": 8947, "pid": 1234, "token": "synthetic-token"}))
            reports = {
                "startup-1234.json": json.dumps({"port": 8947, "pid": 1234,
                                                  "phase": "ready"}),
                "startup-5678.json": "{incomplete startup report",
                "diagnostic.json": json.dumps({"port": 8947, "pid": 1234}),
            }
            for name, value in reports.items():
                (root / name).write_text(value)
            with mock.patch("lighttable_cli.instances.process_is_alive",
                            return_value=True) as alive:
                instances = discover(root)
                self.assertEqual(len(instances), 1)
                self.assertEqual(select(instances, port=8947).path, registration)
                alive.assert_called_once_with(1234)
            for name, value in reports.items():
                self.assertEqual((root / name).read_text(), value)

    def test_explicit_port_selects_live_instance_and_keeps_compatible_records(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for port, token in ((8321, None), (8947, "synthetic-token")):
                record = {"port": str(port), "pid": 1234}
                if token is not None:
                    record["token"] = token
                (root / f"{port}.json").write_text(json.dumps(record))
            (root / "startup-1234.json").write_text(json.dumps({
                "port": 8947, "pid": 1234}))
            with mock.patch("lighttable_cli.instances.process_is_alive", return_value=True):
                instances = discover(root)
            with self.assertRaisesRegex(RuntimeError, "several"):
                select(instances)
            self.assertEqual(select(instances, port=8947).token, "synthetic-token")
            self.assertEqual(select(instances, port=8321).token, "")
            self.assertIsNone(select(instances, port=9000))

    def test_invalid_registration_schema_or_port_does_not_create_duplicates(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "8321.json").write_text("[]")
            (root / "8947.json").write_text(json.dumps({"port": 8321, "pid": 1234}))
            with mock.patch("lighttable_cli.instances.process_is_alive") as alive:
                self.assertEqual(discover(root, clean_stale=False), [])
                self.assertEqual(len(list(root.iterdir())), 2)
                self.assertEqual(discover(root), [])
                alive.assert_not_called()
            self.assertEqual(list(root.iterdir()), [])


class CLIContractTests(unittest.TestCase):
    def test_global_flags_work_after_subcommands(self):
        parser = build_parser()
        args = parser.parse_args(normalize_global_arguments(
            ["photos", "list", "--limit", "2", "--json"]))
        self.assertTrue(args.json)
        self.assertEqual(args.limit, 2)

    def test_state_edits_merge_nested_values(self):
        class FakeClient:
            def __init__(self): self.posts = []
            def get(self, _path):
                return {"grade": {"exposure": 0.1, "contrast": 0.2}}
            def post(self, path, body):
                self.posts.append((path, body)); return {"ok": True}
        client = FakeClient()
        args = SimpleNamespace(origin="test")
        result = state_merge_update(client, ["a"],
                                    {"grade": {"exposure": 0.4}},
                                    args, "Edit")
        self.assertEqual(result["count"], 1)
        self.assertEqual(client.posts[0][1]["grade"],
                         {"exposure": 0.4, "contrast": 0.2})

    def test_state_edits_preserve_nested_hsl_bands(self):
        class FakeClient:
            def __init__(self): self.posts = []
            def get(self, _path):
                return {"grade": {"hsl": {
                    "red": {"s": 0.2}, "blue": {"l": -0.1}}}}
            def post(self, path, body):
                self.posts.append((path, body)); return {"ok": True}
        client = FakeClient()
        args = SimpleNamespace(origin="test")
        state_merge_update(client, ["a"],
                           {"grade": {"hsl": {"red": {"h": 0.1}}}},
                           args, "Edit")
        self.assertEqual(client.posts[0][1]["grade"]["hsl"], {
            "red": {"s": 0.2, "h": 0.1}, "blue": {"l": -0.1}})

    def test_curve_grammar_uses_normalized_control_points(self):
        curves = curve_assignments(["L=0,0;0.25,0.2;1,1"])
        self.assertEqual(curves["curveL"],
                         [[0.0, 0.0], [63.75, 51.0], [255.0, 255.0]])
        cleaned, warnings = server.cleaned_state_request(
            {"grade": curves}, strict=True)
        self.assertEqual(warnings, [])
        self.assertEqual(len(cleaned["grade"]["curveL"]), 256)

    def test_domain_dispatch_uses_named_route(self):
        class FakeClient:
            def __init__(self): self.calls = []
            def post(self, path, body):
                self.calls.append(("POST", path, body)); return {"ok": True}
            def get(self, path):
                self.calls.append(("GET", path, None)); return {"ok": True}
        client = FakeClient()
        args = SimpleNamespace(command="prefs", action="set", refs=[],
                               body='{"theme":"dark"}', yes=False,
                               dry_run=False)
        dispatch_domain(client, args)
        self.assertEqual(client.calls,
                         [("POST", "/api/prefs", {"theme": "dark"})])

    def test_destructive_domain_dry_run_never_posts(self):
        class FakeClient:
            def post(self, *_args, **_kwargs):
                raise AssertionError("dry run must not mutate")
        args = SimpleNamespace(command="ai-index", action="clear", refs=[],
                               body="{}", yes=False, dry_run=True)
        result = dispatch_domain(FakeClient(), args)
        self.assertTrue(result["dryRun"])
        self.assertEqual(result["action"], "clear")

    def test_schema_is_self_contained(self):
        document = schema()
        self.assertEqual(document["$schema"],
                         "https://json-schema.org/draft/2020-12/schema")
        self.assertIn("stateUpdate", document["$defs"])
        self.assertIn("job", document["$defs"])

    def test_generated_cli_docs_are_current(self):
        completed = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "gen-cli-docs.py"),
             "--check"], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_every_server_api_route_is_declared(self):
        source = (ROOT / "server.py").read_text(encoding="utf-8")
        actual = set(re.findall(r'u\.path == "(/api/[^"]+)"', source))
        declared = {route for routes in ROUTE_COVERAGE.values()
                    for route in routes if route != "/api/*"}
        declared.update(INTERNAL_ROUTES)
        missing = sorted(route for route in actual if route not in declared)
        self.assertEqual(missing, [])
        declared_static = {route for route in declared if "<" not in route}
        self.assertEqual(sorted(declared_static - actual), [])
        self.assertIn('u.path.startswith("/api/jobs/")', source)
        self.assertIn('u.path.endswith("/cancel")', source)

    def test_release_scripts_package_cli_and_control_modules(self):
        mac = (ROOT / "scripts" / "build-release.sh").read_text()
        win = (ROOT / "scripts" / "windows" /
               "build-release.ps1").read_text()
        self.assertIn("lighttable_cli", mac)
        self.assertIn('Contents/MacOS/lighttable-cli', mac)
        self.assertNotIn('Contents/MacOS/lighttable"', mac)
        direct = (ROOT / "build-app.sh").read_text()
        personal = (ROOT / "scripts" / "update-personal-app.sh").read_text()
        self.assertIn('Contents/MacOS/lighttable-cli', direct)
        self.assertIn('MacOS/lighttable-cli', personal)
        self.assertNotIn('MacOS/lighttable"', direct)
        self.assertNotIn('MacOS/lighttable"', personal)
        signer = (ROOT / "scripts" / "sign-app.sh").read_text()
        self.assertIn('Contents/MacOS/lighttable-cli', signer)
        self.assertIn('sign_path "$APP_CLI"', signer)
        for module in ("events.py", "jobs.py", "validation.py"):
            self.assertIn(module, win)
        self.assertIn("lighttable.cmd", win)

    def test_web_client_installs_event_and_ui_bridges(self):
        app = (ROOT / "web" / "app.js").read_text()
        bridge = (ROOT / "web" / "ui-bridge.js").read_text()
        self.assertIn("from '/web/ui-bridge.js'", app)
        self.assertIn("from '/web/events.js'", bridge)
        self.assertIn("installUIBridge", app)


if __name__ == "__main__":
    unittest.main()


class PresetApplicationTests(unittest.TestCase):
    """A preset applies only the groups it carries, the way the window does."""

    PRESET = {
        "name": "Warm", "includeFilm": False, "params": {},
        "grade": {"exposure": 0.3, "contrast": 0.0},
        "includedGrade": ["exposure"], "masks": [], "heals": [],
        "optics": dict(OPTICS_DEFAULTS),
    }
    PHOTO = {
        "params": {"stock": "kodak_portra_400"}, "grade": {"contrast": 0.1},
        "masks": [{"id": "mask-1", "type": "radial"}],
        "heals": [{"id": "heal-1"}],
        "optics": {**OPTICS_DEFAULTS, "rotate": 2.0},
    }

    def client(self, preset):
        photo = self.PHOTO

        class FakeClient:
            def __init__(self): self.posts = []
            def get(self, path):
                if path == "/api/presets": return [preset]
                return json.loads(json.dumps(photo))
            def post(self, path, body):
                self.posts.append((path, body)); return {"ok": True}
        return FakeClient()

    def arguments(self, **extra):
        base = dict(command="presets", action="apply", name="Warm",
                    refs=["a.jpg"], where=[], names_from=None, limit=10,
                    sort="capture:desc", origin="test")
        base.update(extra)
        return SimpleNamespace(**base)

    def test_cli_lens_defaults_match_the_server(self):
        import edits
        self.assertEqual(OPTICS_DEFAULTS, edits.clean_optics({}))

    def test_a_grade_only_preset_carries_only_its_grade(self):
        self.assertEqual(preset_state(self.PRESET), {"grade": {"exposure": 0.3}})

    def test_applying_a_grade_only_preset_keeps_film_masks_and_lens(self):
        client = self.client(self.PRESET)
        dispatch(client, self.arguments())
        path, body = client.posts[0]
        self.assertEqual(path, "/api/state")
        self.assertEqual(body["grade"], {"contrast": 0.1, "exposure": 0.3})
        for group in ("params", "masks", "heals", "optics"):
            self.assertNotIn(group, body)

    def test_preset_masks_layer_over_the_photo_with_fresh_identities(self):
        preset = dict(self.PRESET, masks=[{"id": "mask-1", "type": "linear"}],
                      optics={**OPTICS_DEFAULTS, "vignette": 0.5})
        client = self.client(preset)
        dispatch(client, self.arguments())
        body = client.posts[0][1]
        self.assertEqual([mask["type"] for mask in body["masks"]],
                         ["radial", "linear"])
        self.assertEqual(body["masks"][0]["id"], "mask-1")
        self.assertNotEqual(body["masks"][1]["id"], "mask-1")
        self.assertEqual(body["optics"],
                         {**OPTICS_DEFAULTS, "rotate": 2.0, "vignette": 0.5})
        self.assertNotIn("heals", body)

    def test_edit_with_a_preset_follows_the_same_rules(self):
        client = self.client(self.PRESET)
        args = SimpleNamespace(
            command="edit", action="set", refs=["a.jpg"], where=[],
            names_from=None, limit=10, sort="capture:desc", origin="test",
            grade=[], film=[], rotate=None, curve=[], hsl=[], crop=None,
            patch=None, replace=None, copy_from=None,
            include="film,grade,crop,masks,heals,optics", preset="Warm",
            layer=False, group="all", history_label="Preset")
        dispatch(client, args)
        body = client.posts[0][1]
        self.assertEqual(body["grade"], {"contrast": 0.1, "exposure": 0.3})
        for group in ("params", "masks", "heals", "optics"):
            self.assertNotIn(group, body)
