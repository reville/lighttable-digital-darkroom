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
    build_parser,
    curve_assignments,
    dispatch_domain,
    normalize_global_arguments,
    state_merge_update,
)
from lighttable_cli.instances import discover
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
            (root / "bad.json").write_text("not json", encoding="utf-8")
            (root / "dead.json").write_text(json.dumps({
                "port": 9, "pid": 99999999, "token": "x"}), encoding="utf-8")
            with mock.patch("lighttable_cli.instances.process_is_alive",
                            return_value=False):
                self.assertEqual(discover(root), [])
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
