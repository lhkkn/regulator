"""Unit tests for conductor/backup.py — off-machine backup freshness.

The module shells out to `security` (via conductor.keychain) and `restic`; tests patch
both so they run offline. Defensiveness contract: a missing restic, missing credentials, a
failed run, or an unparseable snapshot degrades to ok=False with a reason — never raises.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datetime import UTC

from conductor import backup, keychain  # noqa: E402


class _FakeProc:
    """Minimal Popen stand-in: communicate() returns the canned output."""

    def __init__(self, stdout="", stderr="", code=0):
        self.args = ["restic"]
        self.returncode = code
        self.pid = 4242
        self._out, self._err = stdout, stderr

    def communicate(self, timeout=None):
        return self._out, self._err


def _popen(stdout="", stderr="", code=0):
    return lambda *a, **k: _FakeProc(stdout, stderr, code)


class BackupTest(unittest.TestCase):
    def setUp(self):
        backup._MEMO = None
        self._real_bin = backup._restic_bin
        self._real_kc = keychain.get
        self._real_popen = backup.subprocess.Popen
        self._real_killpg = backup.os.killpg

    def tearDown(self):
        backup._restic_bin = self._real_bin
        keychain.get = self._real_kc
        backup.subprocess.Popen = self._real_popen
        backup.os.killpg = self._real_killpg
        backup._MEMO = None

    def test_no_restic(self):
        backup._restic_bin = lambda: None
        rec = backup.latest(fresh=True)
        self.assertFalse(rec["ok"])
        self.assertEqual(rec["error"], "restic not installed")

    def test_missing_credentials(self):
        backup._restic_bin = lambda: "/opt/homebrew/bin/restic"
        keychain.get = lambda service, account: None
        rec = backup.latest(fresh=True)
        self.assertFalse(rec["ok"])
        self.assertEqual(rec["error"], "keychain credentials missing")

    def test_parses_latest_snapshot(self):
        backup._restic_bin = lambda: "/opt/homebrew/bin/restic"
        keychain.get = lambda service, account: "value"
        backup.subprocess.Popen = _popen(stdout=json.dumps([{"time": "2026-09-19T12:00:00Z"}]))
        rec = backup.latest(fresh=True)
        self.assertTrue(rec["ok"])
        self.assertEqual(rec["time"], "2026-09-19T12:00:00Z")
        self.assertIsNotNone(rec["age_hours"])

    def test_no_snapshots(self):
        backup._restic_bin = lambda: "/opt/homebrew/bin/restic"
        keychain.get = lambda service, account: "value"
        backup.subprocess.Popen = _popen(stdout="[]")
        rec = backup.latest(fresh=True)
        self.assertFalse(rec["ok"])
        self.assertEqual(rec["error"], "no snapshots")

    def test_restic_failure_is_reported(self):
        backup._restic_bin = lambda: "/opt/homebrew/bin/restic"
        keychain.get = lambda service, account: "value"
        backup.subprocess.Popen = _popen(code=1, stderr="boom")
        rec = backup.latest(fresh=True)
        self.assertFalse(rec["ok"])
        self.assertIn("boom", rec["error"])

    def test_timeout_never_raises(self):
        backup._restic_bin = lambda: "/opt/homebrew/bin/restic"
        keychain.get = lambda service, account: "value"
        backup.os.killpg = lambda *a, **k: None  # never kill a real pid

        class _Hang(_FakeProc):
            def communicate(self, timeout=None):
                if timeout is not None:
                    raise subprocess.TimeoutExpired(cmd="restic", timeout=timeout)
                return self._out, self._err

        backup.subprocess.Popen = lambda *a, **k: _Hang()
        rec = backup.latest(fresh=True)
        self.assertFalse(rec["ok"])
        self.assertEqual(rec["error"], "restic timed out")


class DrillTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._env = os.environ.get("CONDUCTOR_APP_DIR")
        os.environ["CONDUCTOR_APP_DIR"] = self.tmp.name

    def tearDown(self):
        if self._env is None:
            os.environ.pop("CONDUCTOR_APP_DIR", None)
        else:
            os.environ["CONDUCTOR_APP_DIR"] = self._env
        self.tmp.cleanup()

    def test_missing_drill(self):
        rec = backup.drill()
        self.assertFalse(rec["ok"])
        self.assertEqual(rec["error"], "no drill recorded")

    def test_valid_drill(self):
        from datetime import datetime

        now = datetime.now(UTC).isoformat()
        Path(backup._drill_path()).write_text(json.dumps({"restored_at": now, "verified": True}))
        rec = backup.drill()
        self.assertTrue(rec["ok"])
        self.assertLess(rec["age_days"], 0.01)

    def test_malformed_drill(self):
        Path(backup._drill_path()).write_text("{not json")
        rec = backup.drill()
        self.assertFalse(rec["ok"])
        self.assertIsNotNone(rec["error"])


if __name__ == "__main__":
    unittest.main()
