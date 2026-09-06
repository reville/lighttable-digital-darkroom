from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr
from unittest import mock

import server
from events import EventBroker


class AcceptedStateEventTests(unittest.TestCase):
    def test_state_events_keep_the_accepted_patch_after_later_writes(self):
        for path in ("/api/state", "/api/state/bulk"):
            with self.subTest(path=path):
                broker = EventBroker()
                subscriber = broker.subscribe("other-window")
                handler = server.Handler.__new__(server.Handler)
                handler.path = path
                handler.headers = {"X-LightTable-Client": "cli-test"}
                handler._enforce_security = mock.Mock()
                handler._json = mock.Mock()
                handler._log_request = mock.Mock()
                patch = {"rating": 9, "status": "approved"}
                body = ({"name": "a.jpg", **patch} if path == "/api/state"
                        else {"names": ["a.jpg", "b.jpg"], "entry": patch})
                handler._body = lambda: {**body, "origin": "cli"}
                accepted = {}

                def save_one(name, entry):
                    accepted[name] = entry

                def save_many(entries):
                    accepted.update(entries)

                with mock.patch.object(server, "EVENTS", broker), \
                        mock.patch.object(server, "catalog_handle", return_value=None), \
                        mock.patch.object(server, "catalog_image_id", return_value=None), \
                        mock.patch.object(server, "save_image_state", side_effect=save_one), \
                        mock.patch.object(server, "save_image_states", side_effect=save_many):
                    handler.do_POST()
                self.assertTrue(handler._json.call_args.args[0]["ok"])
                event = broker.get(subscriber, timeout=0)
                self.assertEqual(event["patch"], {"rating": 5, "status": "approved"})
                self.assertEqual(event["names"], list(accepted))
                self.assertEqual(event["client"], "cli-test")
                self.assertEqual(event["fields"], ["rating", "status"])
                # The event must not change if a following save modifies the
                # stored state before the other window consumes its update.
                for entry in accepted.values():
                    entry["rating"] = 1
                self.assertEqual(event["patch"]["rating"], 5)


class QuietDisconnectTests(unittest.TestCase):
    """Client hang-ups must not bury real errors under socket tracebacks."""

    def setUp(self):
        self.httpd = server.LightTableServer(("127.0.0.1", 0), server.Handler)
        self.addCleanup(self.httpd.server_close)

    def _handle(self, exc: BaseException) -> str:
        captured = io.StringIO()
        with redirect_stderr(captured):
            try:
                raise exc
            except Exception:  # noqa: BLE001 - mirrors socketserver's path
                self.httpd.handle_error(None, ("127.0.0.1", 1))
        return captured.getvalue()

    def test_connection_resets_are_silent(self):
        for exc in (ConnectionResetError(54, "Connection reset by peer"),
                    BrokenPipeError(32, "Broken pipe"),
                    ConnectionAbortedError(53, "Software caused abort")):
            with self.subTest(exc=type(exc).__name__):
                self.assertEqual(self._handle(exc), "")

    def test_other_handler_failures_still_report(self):
        output = self._handle(ValueError("unexpected request state"))
        self.assertIn("ValueError", output)
        self.assertIn("unexpected request state", output)


if __name__ == "__main__":
    unittest.main()
