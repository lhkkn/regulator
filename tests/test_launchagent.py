"""Unit tests for conductor/launchagent.py — one plist renderer for every scheduled job."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conductor import launchagent  # noqa: E402


class RenderTest(unittest.TestCase):
    def test_render_has_label_and_program(self):
        xml = launchagent.render("com.x.job", ["/bin/echo", "hi"])
        self.assertIn("<string>com.x.job</string>", xml)
        self.assertIn("<string>/bin/echo</string>", xml)
        self.assertIn("<string>hi</string>", xml)
        self.assertTrue(xml.startswith("<?xml"))
        self.assertIn("</plist>", xml)

    def test_render_includes_optional_keys(self):
        xml = launchagent.render(
            "com.x.job",
            ["/bin/true"],
            interval=600,
            run_at_load=True,
            keep_alive=True,
            workdir="/tmp",
            log="/tmp/job.log",
        )
        self.assertIn("<integer>600</integer>", xml)
        self.assertIn("<key>RunAtLoad</key>", xml)
        self.assertIn("<key>KeepAlive</key>", xml)
        self.assertIn("<key>WorkingDirectory</key>", xml)
        self.assertIn("/tmp/job.log", xml)

    def test_omitted_options_absent(self):
        xml = launchagent.render("com.x.job", ["/bin/true"])
        self.assertNotIn("StartInterval", xml)
        self.assertNotIn("KeepAlive", xml)


class InstallTest(unittest.TestCase):
    def test_install_writes_under_override_and_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"CONDUCTOR_LAUNCH_AGENTS": tmp}):
                with mock.patch.object(launchagent.subprocess, "run") as run:
                    target = launchagent.install("com.x.job", "<plist/>")
                    self.assertEqual(Path(target), Path(tmp) / "com.x.job.plist")
                    self.assertTrue(Path(target).is_file())
                    self.assertGreaterEqual(run.call_count, 2)  # unload + load
                    launchagent.uninstall("com.x.job")
                    self.assertFalse(Path(target).exists())


if __name__ == "__main__":
    unittest.main()
