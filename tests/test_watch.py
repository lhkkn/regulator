"""Unit tests for conductor/watch.py — the proactive loop (checks injected, tmp state)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conductor import watch  # noqa: E402
from tests.support import EnvTestCase  # noqa: E402


class WatchTest(EnvTestCase):
    def setUp(self):
        super().setUp()
        self.set_env(CONDUCTOR_APP_DIR=self.tmp.name)
        self.addCleanup(setattr, watch, "CHECKS", watch.CHECKS)

    def test_run_records_findings_and_heartbeat(self):
        watch.CHECKS = {"a": lambda: (True, "ok a"), "b": lambda: (False, "bad b")}
        result = watch.run_once(escalate=False)
        self.assertEqual(result["findings"], ["bad b"])
        self.assertTrue(watch.heartbeat()["alive"])
        self.assertIn("watch: last run", watch.status())

    def test_consecutive_failures_accumulate(self):
        watch.CHECKS = {"x": lambda: (False, "nope")}
        watch.run_once(escalate=False)
        result = watch.run_once(escalate=False)
        self.assertEqual(result["checks"]["x"]["failures"], 2)

    def test_recovery_resets_failures(self):
        watch.CHECKS = {"x": lambda: (False, "nope")}
        watch.run_once(escalate=False)
        watch.CHECKS = {"x": lambda: (True, "fine")}
        result = watch.run_once(escalate=False)
        self.assertEqual(result["checks"]["x"]["failures"], 0)
        self.assertEqual(result["findings"], [])

    def test_never_run_status(self):
        self.assertIn("never run", watch.status())

    def test_check_exception_is_contained(self):
        def boom():
            raise RuntimeError("kaboom")

        watch.CHECKS = {"bad": boom}
        result = watch.run_once(escalate=False)
        self.assertFalse(result["checks"]["bad"]["ok"])
        self.assertIn("kaboom", result["findings"][0])


class AdminCheckTest(EnvTestCase):
    def test_missing(self):
        self.set_env(HOME=self.tmp.name)
        ok, detail = watch._check_admin()
        self.assertFalse(ok)
        self.assertIn("missing", detail)

    def test_present(self):
        self.set_env(HOME=self.tmp.name)
        d = Path(self.tmp.name) / "admin" / "01_Identity"
        d.mkdir(parents=True)
        (d / "passport.pdf").write_text("x")
        ok, detail = watch._check_admin()
        self.assertTrue(ok)
        self.assertIn("1 files", detail)

    def test_empty(self):
        self.set_env(HOME=self.tmp.name)
        (Path(self.tmp.name) / "admin").mkdir()
        ok, detail = watch._check_admin()
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
