"""Unit tests for conductor/worktree.py — discovery + the add/remove contracts.

Uses a real temporary git repo (git is a hard dependency of the feature), so the
porcelain parse and the actuator round-trip are exercised for real, not mocked.
"""

import os
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conductor import contracts, worktree  # noqa: E402
from conductor.projects import reconcile  # noqa: E402
from tests.support import EnvTestCase, write_registry  # noqa: E402


def git(d: Path, *args: str) -> str:
    # Strip git's own env (GIT_DIR/GIT_INDEX_FILE/…): inherited from a hook, they would
    # retarget `git -C <tmp>` at the outer repo.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(["git", "-C", str(d), *args], check=True, capture_output=True, text=True, env=env).stdout


class WorktreeTest(EnvTestCase):
    def setUp(self):
        super().setUp()
        self.set_env(CONDUCTOR_APP_DIR=str(self.tmpdir))
        ws = self.tmpdir / "ws"
        (ws / ".workspace-root").mkdir(parents=True)
        (ws / "registry").mkdir()
        self.reg = ws / "registry" / "registry.toml"
        write_registry(self.reg, [{"name": "demo", "status": "active", "tier": "personal", "deploy": "none"}])
        self.ws = ws.resolve()
        self.repo = ws / "work" / "apps" / "demo"
        self.repo.mkdir(parents=True)
        git(self.repo, "init")
        (self.repo / "README.md").write_text("hello\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "init")
        self.set_env(CONDUCTOR_WORKSPACE_ROOT=str(ws), CONDUCTOR_REGISTRY_FILE=str(self.reg))

    def test_no_linked_worktrees_initially(self):
        self.assertEqual(worktree.linked("demo"), [])
        self.assertEqual(worktree.discover(), [])
        self.assertIn("single-writer", worktree.render())

    def test_discovers_a_linked_worktree_and_flags_dirty_shared_tree(self):
        sibling = self.repo.parent / "demo-task"
        git(self.repo, "worktree", "add", str(sibling), "-b", "agent/task")

        found = worktree.discover()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["project"], "demo")
        self.assertEqual(len(found[0]["worktrees"]), 1)
        self.assertEqual(found[0]["worktrees"][0]["branch"], "agent/task")
        self.assertIn("agent/task", worktree.render("demo"))

        # clean shared checkout → informational OK only
        clean = worktree.detect([{"name": "demo", "status": "active"}])
        self.assertEqual([t for t, _ in clean], [worktree.OK])

        # dirty shared checkout while a worktree is active → WARN (the failure mode)
        (self.repo / "README.md").write_text("edited\n", encoding="utf-8")
        with_risk = worktree.detect([{"name": "demo", "status": "active"}])
        self.assertIn(worktree.WARN, [t for t, _ in with_risk])

    def test_is_linked_distinguishes_the_two_kinds_of_checkout(self):
        """One character of filesystem: `.git` is a dir in a checkout, a file in a worktree."""
        sibling = self.repo.parent / "demo-task"
        git(self.repo, "worktree", "add", str(sibling), "-b", "agent/task")
        self.assertTrue(worktree.is_linked(sibling))
        self.assertFalse(worktree.is_linked(self.repo))
        # both are repos — `is_repo` is the weaker question, and it was the one being asked
        self.assertTrue(worktree.is_repo(sibling))
        self.assertTrue(worktree.is_repo(self.repo))

    def test_a_linked_worktree_is_not_an_unregistered_project(self):
        """The regression this exists for: `trainer-api` is `trainer`'s `serve-api` worktree.

        `registry_drift` walks work/apps for project directories and reported every linked
        worktree as "on disk but not in registry.toml" — a WARN that could not be cleared
        by any action, and so one that taught the reader to skip that section.
        """
        from conductor.projects import checks

        sibling = self.repo.parent / "demo-api"
        git(self.repo, "worktree", "add", str(sibling), "-b", "agent/api")

        issues = checks.registry_drift([{"name": "demo", "status": "active", "deploy": "none"}])
        self.assertEqual([i for i in issues if "demo-api" in i], [])

    def test_a_genuinely_unregistered_dir_is_still_flagged(self):
        """The fix must not blind the check to a real missing entry."""
        from conductor.projects import checks

        (self.repo.parent / "orphan").mkdir()

        issues = checks.registry_drift([{"name": "demo", "status": "active", "deploy": "none"}])
        self.assertTrue(any("orphan" in i for i in issues), issues)

    def test_add_and_remove_contract_round_trip(self):
        dry = contracts.execute("worktree.add", {"project": "demo", "task": "t1"})
        self.assertEqual(dry["outcome"], "dry-run")
        self.assertFalse((self.repo.parent / "demo-t1").exists())

        made = contracts.execute("worktree.add", {"project": "demo", "task": "t1", "apply": True})
        self.assertEqual(made["outcome"], "verified", made)
        self.assertTrue((self.repo.parent / "demo-t1").is_dir())
        self.assertIn("agent/t1", git(self.repo, "branch", "--list"))

        gone = contracts.execute("worktree.remove", {"project": "demo", "task": "t1", "apply": True})
        self.assertEqual(gone["outcome"], "verified", gone)
        self.assertFalse((self.repo.parent / "demo-t1").exists())
        self.assertIn("agent/t1", git(self.repo, "branch", "--list"))  # branch survives

    def test_add_refuses_when_branch_exists(self):
        git(self.repo, "branch", "agent/dup")
        result = contracts.execute("worktree.add", {"project": "demo", "task": "dup"})
        self.assertEqual(result["outcome"], "refused")
        self.assertIn("already exists", result["precondition"]["detail"])

    def test_reconcile_reports_worktrees_section(self):
        code, text = reconcile.run(local=True)
        self.assertIn("Worktrees (parallel agents)", text)
        self.assertEqual(code, 0)  # no worktree yet → no warning


if __name__ == "__main__":
    unittest.main()
