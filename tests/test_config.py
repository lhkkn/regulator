"""Unit tests for conductor/config.py — env-overridable paths (testability seam).

Paths are functions so a test can redirect the whole system into a temp dir; that's the
seam the other modules' tests rely on.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conductor import config  # noqa: E402


class ConfigTest(unittest.TestCase):
    def test_app_dir_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CONDUCTOR_APP_DIR", None)
            self.assertEqual(config.app_dir(), Path.home() / "Library" / "Application Support" / "conductor")

    def test_app_dir_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"CONDUCTOR_APP_DIR": tmp}):
                self.assertEqual(config.app_dir(), Path(tmp))
                self.assertEqual(config.state_file("x.json"), Path(tmp) / "x.json")

    def test_launch_agents_and_logs_override(self):
        with mock.patch.dict(os.environ, {"CONDUCTOR_LAUNCH_AGENTS": "/tmp/la", "CONDUCTOR_LOGS": "/tmp/logs"}):
            self.assertEqual(config.launch_agents_dir(), Path("/tmp/la"))
            self.assertEqual(config.logs_dir(), Path("/tmp/logs"))

    def test_thresholds_are_typed(self):
        self.assertIsInstance(config.SPEND_FLAG_USD, float)
        self.assertIsInstance(config.BACKUP_STALE_HOURS, int)
        self.assertIsInstance(config.WATCH_INTERVAL_SECONDS, int)


if __name__ == "__main__":
    unittest.main()
