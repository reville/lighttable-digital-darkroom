# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import os
import subprocess
import sys
import unittest

from lighttable_cli.instances import process_is_alive


class ProcessLivenessTests(unittest.TestCase):
    def test_current_and_invalid_processes(self):
        self.assertTrue(process_is_alive(os.getpid()))
        self.assertFalse(process_is_alive(0))
        self.assertFalse(process_is_alive(-1))
        self.assertFalse(process_is_alive(0x7FFFFFFF))

    def test_exited_child_is_dead_even_while_its_process_handle_is_retained(self):
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            self.assertTrue(process_is_alive(child.pid))
            self.assertIsNone(child.poll(), "A liveness check must not signal the child")
        finally:
            child.terminate()
            child.wait(timeout=5)
        # Keep the Popen object and its Windows process handle open here.
        self.assertFalse(process_is_alive(child.pid))


if __name__ == "__main__":
    unittest.main()
