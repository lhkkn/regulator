"""Unit tests for conductor/maintenance.py — the contract-driven safe fixes + digest."""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conductor import maintenance  # noqa: E402
from tests.support import EnvTestCase  # noqa: E402


class FakeContract:
    name = "fake"
    tool = "conductor.fake"
    description = "fake fix"

    @staticmethod
    def _noop(*_a):
        return (True, "")

    precondition = _noop
    act = _noop
    verify = _noop


class DigestTest(unittest.TestCase):
    def test_render_digest(self):
        d = maintenance.render_digest({"fixes": ["pruned Docker"], "flags": ["keys overdue"]})
        self.assertIn("fixed:", d)
        self.assertIn("pruned Docker", d)
        self.assertIn("needs you:", d)
        self.assertIn("keys overdue", d)

    def test_render_digest_all_clear(self):
        self.assertEqual(maintenance.render_digest({"fixes": [], "flags": []}), "nothing to do — all clear")


class RunTest(EnvTestCase):
    def setUp(self):
        super().setUp()
        self.set_env(CONDUCTOR_APP_DIR=self.tmp.name)
        self.addCleanup(setattr, maintenance, "_MAINTENANCE", maintenance._MAINTENANCE)
        maintenance._MAINTENANCE = ("fake",)
        self.addCleanup(setattr, maintenance.contracts, "get", maintenance.contracts.get)
        maintenance.contracts.get = lambda name: FakeContract()

    def test_dry_run_reports_due_and_does_not_apply(self):
        calls = []

        def fake_run(contract, args=None, *, apply=False, actor_name=None):
            calls.append(apply)
            return {"precondition": {"ok": True, "detail": "due"}, "outcome": "dry-run", "effect": None, "ok": True}

        with (
            mock.patch.object(maintenance.actuator, "run", side_effect=fake_run),
            mock.patch("conductor.brief.build_brief", return_value={"flags": []}),
        ):
            result = maintenance.run(dry=True)
        self.assertEqual(calls, [False])
        self.assertEqual(result["fixes"], ["would fake fix"])

    def test_run_applies_and_reports_verified(self):
        def fake_run(contract, args=None, *, apply=False, actor_name=None):
            return {
                "precondition": {"ok": True, "detail": "due"},
                "outcome": "verified",
                "effect": {"summary": "did fake"},
                "ok": True,
            }

        with (
            mock.patch.object(maintenance.actuator, "run", side_effect=fake_run) as m,
            mock.patch("conductor.brief.build_brief", return_value={"flags": []}),
        ):
            result = maintenance.run()
        self.assertEqual(result["fixes"], ["did fake"])
        self.assertTrue(m.call_args.kwargs["apply"])

    def test_failure_becomes_a_flag(self):
        def fake_run(contract, args=None, *, apply=False, actor_name=None):
            return {"precondition": {"ok": True, "detail": "due"}, "outcome": "flagged", "effect": None, "ok": False}

        with (
            mock.patch.object(maintenance.actuator, "run", side_effect=fake_run),
            mock.patch("conductor.brief.build_brief", return_value={"flags": ["other"]}),
        ):
            result = maintenance.run()
        self.assertTrue(any("fake" in f for f in result["flags"]))


if __name__ == "__main__":
    unittest.main()
