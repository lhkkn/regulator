"""Unit tests for conductor/runs.py — schedule parsing, attribution, gate parsing."""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conductor import runs  # noqa: E402
from tests.support import EnvTestCase  # noqa: E402


class ScheduleTest(unittest.TestCase):
    def test_start_interval(self):
        self.assertEqual(runs._la_schedule({"StartInterval": 3600}), "every 3600s")

    def test_calendar_dict(self):
        s = runs._la_schedule({"StartCalendarInterval": {"Hour": 3, "Minute": 5}})
        self.assertEqual(s, "at 03:05")

    def test_calendar_list_with_weekday(self):
        s = runs._la_schedule({"StartCalendarInterval": [{"Hour": 4, "Minute": 0, "Weekday": 1}]})
        self.assertEqual(s, "wd 1 at 04:00")

    def test_no_schedule(self):
        self.assertIsNone(runs._la_schedule({}))


class CategoryTest(unittest.TestCase):
    def test_each_category(self):
        self.assertEqual(runs._category("verify-gate", "conductor"), "verify")
        self.assertEqual(runs._category("backup.sh", "infra"), "backup")
        self.assertEqual(runs._category("send_notification.sh", "cinema"), "notify")
        self.assertEqual(runs._category("watch", "conductor"), "monitor")
        self.assertEqual(runs._category("logrotate", "infra"), "maintenance")
        self.assertEqual(runs._category("run_scrape.py", "cinema"), "data")
        self.assertEqual(runs._category("serve", "media"), "app")


class OwnerTest(unittest.TestCase):
    def setUp(self):
        self.patches = [
            mock.patch("conductor.projects.checks._handles", return_value=(set(), {}, {})),
            mock.patch("conductor.projects.checks._attribute", return_value=None),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_alias_attribution(self):
        with mock.patch("conductor.runs.config.value", return_value={"media": "media"}):
            self.assertEqual(runs._owner("", "media deploy", ""), "media")

    def test_defaults_to_infra(self):
        self.assertEqual(runs._owner("", "zzz", ""), "infra")


class GateTest(EnvTestCase):
    def setUp(self):
        super().setUp()
        self.set_env(CONDUCTOR_APP_DIR=self.tmp.name)

    def test_gate_parses_check_runs_newest_first(self):
        path = Path(self.tmp.name) / "check-runs.jsonl"
        path.write_text(
            json.dumps({"ts": 100, "ms": 5, "rc": 0, "failures": []})
            + "\n"
            + json.dumps({"ts": 200, "ms": 9, "rc": 1, "failures": ["a", "b"]})
            + "\n"
        )
        job = runs._gate_job()
        self.assertEqual(job["id"], "verify-gate")
        self.assertEqual(job["last"]["status"], "fail")
        self.assertIn("2 failure(s)", job["last"]["detail"])
        self.assertEqual(len(job["runs"]), 2)

    def test_gate_with_no_runs(self):
        self.assertIsNone(runs._gate_job()["last"])


class JobLineTest(unittest.TestCase):
    def test_job_line_renders(self):
        j = {
            "name": "com.conductor.check",
            "kind": "gate",
            "trigger": "event",
            "schedule": None,
            "last": {"status": "ok", "started": None, "detail": "exit 0"},
        }
        line = runs._job_line(j)
        self.assertIn("com.conductor.check", line)
        self.assertIn("event", line)


if __name__ == "__main__":
    unittest.main()
