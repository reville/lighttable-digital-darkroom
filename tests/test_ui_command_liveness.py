# SPDX-License-Identifier: GPL-3.0-only
"""A window that is listening can be driven, even if its heartbeat is late.

The window reports its state on a timer, and browsers throttle timers in a
window that is not in front, so an idle background window's report goes stale
within seconds while the event stream it answers commands on stays open. Gating
on the report alone refused every command to such a window.
"""
from __future__ import annotations

import time
import unittest
from unittest import mock

import server
from events import EventBroker


def command_handler(body):
    handler = server.Handler.__new__(server.Handler)
    handler.path = "/api/ui/command"
    handler.headers = {}
    handler._enforce_security = mock.Mock()
    handler._json = mock.Mock()
    handler._log_request = mock.Mock()
    handler._body = lambda: body
    return handler


class UICommandLivenessTests(unittest.TestCase):
    def drive(self, *, reported_at, subscribed, allow=True):
        broker = EventBroker()
        if subscribed:
            broker.subscribe("window-1")
        state = {"client": "window-1", "reportedAt": reported_at,
                 "allowAutomation": allow}
        handler = command_handler({"command": "zoomFit", "timeout": 0.1})
        with mock.patch.object(server, "EVENTS", broker), \
                mock.patch.dict(server.UI_STATE, state, clear=True):
            handler.do_POST()
        return handler._json.call_args.args

    def test_a_throttled_heartbeat_still_accepts_a_command(self):
        payload, status = self.drive(reported_at=time.time() - 600,
                                     subscribed=True)
        # The command is delivered; it times out here only because no window is
        # really there to answer it. What matters is that it was not refused.
        self.assertNotEqual(payload.get("code"), "no-window")

    def test_no_stream_and_a_stale_report_is_refused(self):
        payload, status = self.drive(reported_at=time.time() - 600,
                                     subscribed=False)
        self.assertEqual(status, 409)
        self.assertEqual(payload["code"], "no-window")

    def test_a_fresh_report_alone_is_still_enough(self):
        payload, status = self.drive(reported_at=time.time(), subscribed=False)
        self.assertNotEqual(payload.get("code"), "no-window")

    def test_automation_can_still_be_refused_by_the_window(self):
        payload, status = self.drive(reported_at=time.time(), subscribed=True,
                                     allow=False)
        self.assertEqual(status, 403)
        self.assertEqual(payload["code"], "automation-disabled")


class ClientConnectedTests(unittest.TestCase):
    def test_the_broker_reports_a_named_client_as_connected(self):
        broker = EventBroker()
        broker.subscribe("window-1")
        self.assertTrue(broker.client_connected("window-1"))
        self.assertFalse(broker.client_connected("window-2"))
        self.assertFalse(broker.client_connected(""))

    def test_an_unsubscribed_client_is_no_longer_connected(self):
        broker = EventBroker()
        subscriber = broker.subscribe("window-1")
        broker.unsubscribe(subscriber)
        self.assertFalse(broker.client_connected("window-1"))


if __name__ == "__main__":
    unittest.main()
