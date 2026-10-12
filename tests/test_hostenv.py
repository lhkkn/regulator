"""Unit tests for conductor/hostenv.py — the OS seam's primitives."""

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conductor import hostenv  # noqa: E402


def _cp(rc=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=stderr)


class HostTest(unittest.TestCase):
    def test_platform_predicates(self):
        self.assertEqual(hostenv.platform_name(), sys.platform)
        self.assertEqual(hostenv.is_macos(), sys.platform == "darwin")

    def test_ssh_passthrough(self):
        with mock.patch.object(hostenv.subprocess, "run", return_value=_cp(stdout="ok")) as run:
            self.assertEqual(hostenv.ssh("h", "true", timeout=5).stdout, "ok")
        self.assertEqual(run.call_args[0][0][0], "ssh")

    def test_ssh_failure_is_synthetic_not_a_raise(self):
        with mock.patch.object(hostenv.subprocess, "run", side_effect=OSError("no ssh")):
            r = hostenv.ssh("h", "true")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no ssh", r.stderr)

    def test_launchctl_list_degrades_to_empty(self):
        with mock.patch.object(hostenv.subprocess, "run", side_effect=OSError):
            self.assertEqual(hostenv.launchctl_list(), "")

    def test_plist_json(self):
        with mock.patch.object(hostenv.subprocess, "run", return_value=_cp(stdout='{"Label": "x"}')):
            self.assertEqual(hostenv.plist_json("f.plist"), {"Label": "x"})
        with mock.patch.object(hostenv.subprocess, "run", return_value=_cp(rc=1, stderr="bad")):
            self.assertIsNone(hostenv.plist_json("f.plist"))

    def test_keychain_get_delegates(self):
        with mock.patch.object(hostenv.keychain, "get", return_value="v") as get:
            self.assertEqual(hostenv.keychain_get("s", "a"), "v")
        get.assert_called_once_with("s", "a")


if __name__ == "__main__":
    unittest.main()
