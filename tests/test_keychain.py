"""Unit tests for conductor/keychain.py — the shared macOS Keychain reader."""

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conductor import keychain  # noqa: E402


class KeychainTest(unittest.TestCase):
    def test_reads_password(self):
        cp = subprocess.CompletedProcess(args=[], returncode=0, stdout="  s3cret \n", stderr="")
        with mock.patch.object(keychain.subprocess, "run", return_value=cp):
            self.assertEqual(keychain.get("svc", "acct"), "s3cret")

    def test_nonzero_returncode_is_none(self):
        cp = subprocess.CompletedProcess(args=[], returncode=44, stdout="", stderr="not found")
        with mock.patch.object(keychain.subprocess, "run", return_value=cp):
            self.assertIsNone(keychain.get("svc", "acct"))

    def test_empty_password_is_none(self):
        cp = subprocess.CompletedProcess(args=[], returncode=0, stdout="\n", stderr="")
        with mock.patch.object(keychain.subprocess, "run", return_value=cp):
            self.assertIsNone(keychain.get("svc", "acct"))

    def test_timeout_never_raises(self):
        with mock.patch.object(
            keychain.subprocess, "run", side_effect=subprocess.TimeoutExpired(cmd="security", timeout=1)
        ):
            self.assertIsNone(keychain.get("svc", "acct"))


if __name__ == "__main__":
    unittest.main()
