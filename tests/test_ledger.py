"""Unit tests for conductor/ledger.py — the hash-chained action ledger (tmp app dir)."""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conductor import ledger  # noqa: E402
from tests.support import EnvTestCase  # noqa: E402


def _entry(**kw):
    base = dict(
        tool="backup_run",
        action="backup.run",
        args={},
        precondition={"ok": True, "detail": "due"},
        effect={"x": 1},
        verify={"ok": True, "detail": "fresh"},
        outcome="verified",
    )
    base.update(kw)
    return base


class LedgerTest(EnvTestCase):
    def setUp(self):
        super().setUp()
        self.set_env(CONDUCTOR_APP_DIR=self.tmp.name)

    def test_missing_ledger_is_intact(self):
        ok, detail = ledger.verify()
        self.assertTrue(ok)
        self.assertIn("no ledger", detail)

    def test_append_builds_a_chain(self):
        ledger.append(**_entry())
        ledger.append(**_entry(action="projects.register"))
        entries = ledger.load()
        self.assertEqual([e["seq"] for e in entries], [0, 1])
        self.assertEqual(entries[0]["prev"], "0" * 64)
        self.assertEqual(entries[1]["prev"], entries[0]["hash"])
        ok, detail = ledger.verify()
        self.assertTrue(ok, detail)

    def test_secret_args_are_redacted(self):
        ledger.append(**_entry(args={"api_token": "supersecret", "project": "foo"}))
        stored = ledger.load()[0]["args"]
        self.assertEqual(stored["api_token"], "***")
        self.assertEqual(stored["project"], "foo")

    def test_long_strings_are_capped(self):
        ledger.append(**_entry(effect={"blob": "x" * 5000}))
        self.assertLessEqual(len(ledger.load()[0]["effect"]["blob"]), ledger._MAX_STR + 1)

    def test_tampering_breaks_the_chain(self):
        ledger.append(**_entry())
        ledger.append(**_entry())
        p = ledger.path()
        lines = p.read_text(encoding="utf-8").splitlines()
        first = json.loads(lines[0])
        first["outcome"] = "verified"  # same? change to something different
        first["effect"] = {"x": 999}
        lines[0] = json.dumps(first)
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        ok, detail = ledger.verify()
        self.assertFalse(ok)
        self.assertIn("hash mismatch", detail)

    def test_truncation_is_detected(self):
        for i in range(3):
            ledger.append(**_entry(effect={"i": i}))
        p = ledger.path()
        lines = p.read_text(encoding="utf-8").splitlines()
        p.write_text(lines[0] + "\n" + lines[2] + "\n", encoding="utf-8")
        ok, detail = ledger.verify()
        self.assertFalse(ok)
        self.assertIn("seq", detail)

    def test_report_never_raises(self):
        self.assertIsInstance(ledger.report(), str)
        ledger.append(**_entry())
        self.assertIn("integrity", ledger.report())


class RotationTest(EnvTestCase):
    def setUp(self):
        super().setUp()
        self.set_env(CONDUCTOR_APP_DIR=self.tmp.name)

    def _fill(self, n=6):
        with mock.patch.object(ledger, "_CAP", 200):
            for i in range(n):
                ledger.append(**_entry(effect={"i": i, "pad": "x" * 40}))

    def test_rotation_keeps_one_continuous_chain(self):
        self._fill()
        segments = sorted(ledger.segment_dir().glob("*.jsonl"))
        self.assertTrue(segments, "expected at least one rotated segment")
        entries = ledger.load()
        self.assertEqual([e["seq"] for e in entries], list(range(6)))
        ok, detail = ledger.verify()
        self.assertTrue(ok, detail)

    def test_tampering_an_archived_segment_breaks_the_chain(self):
        self._fill()
        seg = sorted(ledger.segment_dir().glob("*.jsonl"))[0]
        lines = seg.read_text(encoding="utf-8").splitlines()
        first = json.loads(lines[0])
        first["effect"] = {"tampered": True}
        lines[0] = json.dumps(first)
        seg.write_text("\n".join(lines) + "\n", encoding="utf-8")
        ok, detail = ledger.verify()
        self.assertFalse(ok)
        self.assertIn("hash mismatch", detail)

    def test_deleting_a_segment_is_detected(self):
        self._fill()
        sorted(ledger.segment_dir().glob("*.jsonl"))[0].unlink()
        ok, detail = ledger.verify()
        self.assertFalse(ok)
        self.assertIn("seq", detail)


if __name__ == "__main__":
    unittest.main()
