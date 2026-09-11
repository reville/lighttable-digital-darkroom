# SPDX-License-Identifier: GPL-3.0-only
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from bounded_logging import RotatingLog


class BoundedLoggingTests(unittest.TestCase):
    def test_repeated_errors_and_long_lines_have_bounded_retention(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "server.log"
            sink = RotatingLog(path, max_bytes=1024, backups=2)
            for _ in range(20000):
                sink.write_line(b"bridge unavailable\n")
            sink.write_line(b"bridge reconnected\n")
            sink.close()
            self.assertIn(b"repeated 19999 times", path.read_bytes())
            self.assertIn(b"bridge reconnected", path.read_bytes())
            sink = RotatingLog(path, max_bytes=1024, backups=2)
            for i in range(25):
                sink.write_line(str(i).encode() + b"x" * 5000)
            sink.close()
            logs = list(Path(directory).glob("server.log*"))
            self.assertLessEqual(len(logs), 3)
            self.assertTrue(all(log.stat().st_size <= 1024 for log in logs))

    def test_native_descriptor_writes_and_shutdown_tail_are_captured(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "server.log"
            script = "import bounded_logging,os; bounded_logging.from_environment(); print('python'); os.write(2,b'native diagnostic\\n'); print('final tail',end='')"
            env = dict(os.environ, LIGHTTABLE_LOG_FILE=str(path))
            result = subprocess.run([sys.executable, "-c", script], env=env,
                                    capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            content = path.read_bytes()
            self.assertIn(b"python", content)
            self.assertIn(b"native diagnostic", content)
            self.assertIn(b"final tail", content)
            self.assertEqual(result.stdout, b"")

    def test_oversized_logs_from_older_versions_keep_recent_crash_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'server.log'
            for suffix in ('', '.1', '.2'):
                Path(str(path) + suffix).write_bytes(b'x' * 10000 + b'crash tail')
            sink = RotatingLog(path, max_bytes=1024, backups=2)
            sink.close()
            for log in Path(directory).glob('server.log*'):
                self.assertLessEqual(log.stat().st_size, 1024)
                self.assertTrue(log.read_bytes().endswith(b'crash tail'))
