"""Unit tests for conductor/agent_docs.py — the AGENTS.md/CLAUDE.md convention guard.

scan_project() reads a temp workspace tree: a real CLAUDE.md must be flagged, a proper
symlink must pass, and a dangling symlink must be flagged. No real workspace touched.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conductor import agent_docs  # noqa: E402


class AgentDocsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        self.proj = self.ws / "work" / "apps" / "demo"
        self.proj.mkdir(parents=True)
        self.project = {"name": "demo", "status": "active"}

    def tearDown(self):
        self.tmp.cleanup()

    def test_real_claude_md_is_flagged(self):
        (self.proj / "CLAUDE.md").write_text("# rules\n")
        failures = agent_docs.scan_project(self.project, self.ws)
        self.assertEqual(len(failures), 1)
        self.assertIn("real file", failures[0])

    def test_proper_symlink_passes(self):
        (self.proj / "AGENTS.md").write_text("# rules\n")
        os.symlink("AGENTS.md", self.proj / "CLAUDE.md")
        self.assertEqual(agent_docs.scan_project(self.project, self.ws), [])

    def test_dangling_symlink_is_flagged(self):
        os.symlink("AGENTS.md", self.proj / "CLAUDE.md")  # AGENTS.md never created
        failures = agent_docs.scan_project(self.project, self.ws)
        self.assertEqual(len(failures), 1)

    def test_archived_project_is_skipped(self):
        (self.proj / "CLAUDE.md").write_text("# rules\n")
        archived = {"name": "demo", "status": "archived"}
        self.assertEqual(agent_docs.scan_project(archived, self.ws), [])


if __name__ == "__main__":
    unittest.main()
