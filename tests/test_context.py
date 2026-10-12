"""Unit tests for conductor/context/ — project resolution, state rendering, and the
curated-content-preserving state-file update."""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conductor.context import api, state  # noqa: E402
from tests.support import EnvTestCase  # noqa: E402


class ProjectStateTest(EnvTestCase):
    def setUp(self):
        super().setUp()
        ws = self.tmpdir / "ws"
        (ws / ".workspace-root").mkdir(parents=True)
        (ws / "work" / "apps" / "demo" / ".context").mkdir(parents=True)
        self.set_env(CONDUCTOR_WORKSPACE_ROOT=str(ws))
        self.root = ws.resolve()

    def test_project_dir_resolves_and_unknown_is_none(self):
        self.assertEqual(api._project_dir("demo"), self.root / "work" / "apps" / "demo")
        self.assertIsNone(api._project_dir("nope"))

    def test_project_state_reads_existing_state_md(self):
        sf = self.root / "work" / "apps" / "demo" / ".context" / "state.md"
        sf.write_text("# Custom state\n")
        self.assertEqual(api.project_state("demo"), "# Custom state\n")

    def test_project_state_falls_back_to_rendered_block(self):
        with mock.patch.object(state, "_run", return_value=(1, "")):
            out = api.project_state("demo")
        self.assertIn(state.BEGIN, out)
        self.assertIn("Recent commits", out)

    def test_project_state_unknown_project(self):
        self.assertIn("project not found", api.project_state("nope"))


class StateUpdateTest(EnvTestCase):
    def test_update_creates_file(self):
        p = self.tmpdir / "proj"
        p.mkdir()
        with mock.patch.object(state, "_run", return_value=(1, "")):
            state_file = state.update_state_file(p)
        self.assertTrue(state_file.is_file())
        text = state_file.read_text()
        self.assertIn("# State", text)
        self.assertIn(state.BEGIN, text)
        self.assertIn(state.END, text)

    def test_update_preserves_curated_content_and_is_idempotent(self):
        p = self.tmpdir / "proj"
        (p / ".context").mkdir(parents=True)
        sf = p / ".context" / "state.md"
        sf.write_text("# State\n\ncurated notes\n\n" + state.BEGIN + "\nold\n" + state.END + "\n\ntrailing\n")
        with mock.patch.object(state, "_run", return_value=(1, "")):
            state.update_state_file(p)
            state.update_state_file(p)  # a second run must not duplicate the block
        text = sf.read_text()
        self.assertIn("curated notes", text)
        self.assertIn("trailing", text)
        self.assertEqual(text.count(state.BEGIN), 1)
        self.assertNotIn("\nold\n", text)

    def test_recent_commits_degrades_when_not_a_repo(self):
        with mock.patch.object(state, "_run", return_value=(128, "")):
            self.assertEqual(state.recent_commits(self.tmpdir), state.NOT_REPO)


if __name__ == "__main__":
    unittest.main()
