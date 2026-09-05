from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr

import server


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
