"""Unit tests for conductor/docs_health.py — the docs-sprawl guardrail's scan logic.

scan_project() takes an explicit workspace, so these run against a temp tree with no
env redirect.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conductor import docs_health  # noqa: E402
from tests.support import EnvTestCase  # noqa: E402


class ScanProjectTest(EnvTestCase):
    def _project(self, name):
        d = self.tmpdir / "work" / "apps" / name
        d.mkdir(parents=True)
        return d

    def test_no_index_and_orphan(self):
        d = self._project("demo")
        (d / "docs").mkdir()
        (d / "docs" / "A.md").write_text("lonely")
        row = docs_health.scan_project({"name": "demo", "status": "active"}, self.tmpdir)
        self.assertFalse(row["ok"])
        self.assertTrue(any("no docs index" in f for f in row["failures"]))
        self.assertTrue(any("orphan" in f for f in row["failures"]))

    def test_indexed_and_linked_doc_passes(self):
        d = self._project("demo")
        (d / "docs").mkdir()
        (d / "docs" / "README.md").write_text("see OVERVIEW.md")
        (d / "docs" / "OVERVIEW.md").write_text("content")
        row = docs_health.scan_project({"name": "demo", "status": "active"}, self.tmpdir)
        self.assertTrue(row["ok"], row["failures"])

    def test_unlinked_doc_is_flagged_as_orphan(self):
        d = self._project("demo")
        (d / "docs").mkdir()
        (d / "docs" / "README.md").write_text("index")
        (d / "docs" / "STRAY.md").write_text("not linked")
        row = docs_health.scan_project({"name": "demo", "status": "active"}, self.tmpdir)
        self.assertFalse(row["ok"])
        self.assertTrue(any("orphan" in f for f in row["failures"]))

    def test_archived_project_never_blocks(self):
        d = self._project("demo")
        (d / "docs").mkdir()
        (d / "docs" / "A.md").write_text("x")
        row = docs_health.scan_project({"name": "demo", "status": "archived"}, self.tmpdir)
        self.assertTrue(row["ok"])
        self.assertTrue(all(f.startswith("(archived") for f in row["failures"]))

    def test_missing_directory_is_skipped(self):
        row = docs_health.scan_project({"name": "nope", "status": "active"}, self.tmpdir)
        self.assertEqual(row.get("skipped"), "no local project dir (remote/archive-only)")


class RenderTest(unittest.TestCase):
    def test_render_flags_only_live_failures(self):
        rows = [
            {"name": "a", "status": "active", "ok": False, "count": 3, "failures": ["boom"]},
            {"name": "b", "status": "archived", "ok": True, "count": 1, "failures": ["(archived — y)"]},
        ]
        lines, flags = docs_health.render(rows)
        self.assertTrue(any("[docs] a: boom" in f for f in flags))
        self.assertFalse(any(f.endswith("b: (archived — y)") for f in flags))
        self.assertTrue(any("markdown docs" in line for line in lines))


class ReportTest(unittest.TestCase):
    def test_report_degrades_when_workspace_missing(self):
        with mock.patch.object(docs_health, "find_workspace_root", side_effect=FileNotFoundError("nope")):
            rows = docs_health.report()
        self.assertEqual(rows[0]["status"], "error")
        self.assertFalse(rows[0]["ok"])


if __name__ == "__main__":
    unittest.main()
