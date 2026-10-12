"""Unit tests for conductor/agent_perms.py — the CRP asymmetry must not rot.

Two things fail differently here: the *parsing* (does the frontmatter scan find the bash
block?) and the *rule* (does a broad allow without a deny count as a violation?). The rule
is a pure function so it gets exhaustive cases; the parser gets a realistic fixture.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conductor import agent_perms  # noqa: E402
from tests.support import EnvTestCase  # noqa: E402

REALISTIC = """---
description: test agent
model: deepseek/deepseek-flash
permission:
  edit: deny
  webfetch: deny
  bash:
    "git status*": allow
    "spool *": allow
    # a comment inside the block
    "python3 -m conductor crp answer*": deny
    "python3 -m conductor crp withdraw*": deny
    "python3 -m conductor *": allow
    "*": deny
---

Body text here.
"""


class BashPermsTest(EnvTestCase):
    """Does the indentation-aware frontmatter scan actually find the bash block?"""

    def _write(self, text: str) -> Path:
        p = self.tmpdir / "agent.md"
        p.write_text(text, encoding="utf-8")
        return p

    def test_reads_the_block(self):
        perms = agent_perms.bash_perms(self._write(REALISTIC))
        self.assertEqual(perms["python3 -m conductor *"], "allow")
        self.assertEqual(perms["python3 -m conductor crp answer*"], "deny")
        self.assertEqual(perms["*"], "deny")
        self.assertEqual(perms["git status*"], "allow")

    def test_ignores_the_top_level_permission_keys(self):
        """`edit: deny` is a sibling of `bash:`, not an entry in it."""
        perms = agent_perms.bash_perms(self._write(REALISTIC))
        self.assertNotIn("edit", perms)
        self.assertNotIn("webfetch", perms)

    def test_comments_inside_the_block_are_skipped(self):
        self.assertNotIn("# a comment inside the block", agent_perms.bash_perms(self._write(REALISTIC)))

    def test_no_frontmatter_is_empty(self):
        self.assertEqual(agent_perms.bash_perms(self._write("just body text\n")), {})

    def test_unterminated_frontmatter_is_empty(self):
        self.assertEqual(agent_perms.bash_perms(self._write("---\nmodel: x\n")), {})

    def test_missing_file_is_empty(self):
        self.assertEqual(agent_perms.bash_perms(self.tmpdir / "nope.md"), {})


class ViolationsTest(unittest.TestCase):
    """The rule itself — no filesystem needed."""

    def test_broad_allow_without_a_deny_is_a_violation(self):
        bad = agent_perms.violations({"python3 -m conductor *": "allow", "*": "deny"})
        self.assertEqual(len(bad), 2)  # both answer and withdraw
        self.assertTrue(all("does not deny" in m for m in bad))

    def test_broad_allow_with_both_denies_is_clean(self):
        good = agent_perms.violations(
            {
                "python3 -m conductor crp answer*": "deny",
                "python3 -m conductor crp withdraw*": "deny",
                "python3 -m conductor *": "allow",
                "*": "deny",
            }
        )
        self.assertEqual(good, [])

    def test_half_the_denies_is_still_a_violation(self):
        """Answering denied but withdrawing open would let an agent clear its own queue."""
        half = agent_perms.violations(
            {
                "python3 -m conductor crp answer*": "deny",
                "python3 -m conductor *": "allow",
            }
        )
        self.assertEqual(len(half), 1)
        self.assertIn("crp withdraw", half[0])

    def test_an_agent_that_cannot_run_conductor_is_clean(self):
        """No broad allow means no inherited answer path — nothing to deny."""
        self.assertEqual(agent_perms.violations({"ls *": "allow", "cat *": "allow", "*": "deny"}), [])

    def test_a_deny_on_the_broad_pattern_is_clean(self):
        self.assertEqual(agent_perms.violations({"python3 -m conductor *": "deny"}), [])

    def test_no_permissions_at_all_is_clean(self):
        self.assertEqual(agent_perms.violations({}), [])


class RealAgentsTest(unittest.TestCase):
    """The live agents must satisfy the rule.

    Trivially passes where there is no `~/Stack` layout (CI), which is why the rule is
    tested directly above — this is the belt, not the braces.
    """

    def test_every_agent_respects_the_asymmetry(self):
        failures = agent_perms.report()
        self.assertEqual(failures, [], "\n".join(failures))
