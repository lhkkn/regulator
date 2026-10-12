"""Unit tests for conductor/proc.py — PATH-then-fallback binary lookup."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conductor import proc  # noqa: E402


class ProcTest(unittest.TestCase):
    def test_finds_on_path(self):
        self.assertIsNotNone(proc.find("sh"))

    def test_missing_returns_none(self):
        self.assertIsNone(proc.find("definitely-not-a-real-binary-xyz"))

    def test_falls_back_to_executable_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "mytool"
            exe.write_text("#!/bin/sh\n")
            exe.chmod(0o755)
            self.assertEqual(proc.find("no-such-tool-xyz", str(exe)), str(exe))

    def test_non_executable_fallback_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "notexec"
            f.write_text("x")
            f.chmod(0o644)
            self.assertIsNone(proc.find("no-such-tool-xyz", str(f)))


if __name__ == "__main__":
    unittest.main()
