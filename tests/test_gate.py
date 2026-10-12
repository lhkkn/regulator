"""Regression guard for the verify gate's own check list.

The gate was split into `conductor/gate/`; this locks the assembled list so a future
refactor can't silently drop a check (add a check → update EXPECTED_CHECKS deliberately).
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conductor import check, gate  # noqa: E402

EXPECTED_CHECKS = {
    "check_practice_scaffold",
    "check_package",
    "check_projects",
    "check_project_lifecycle",
    "check_mcp",
    "check_ledger",
    "check_contracts",
    "check_commands",
    "check_keys",
    "check_tokens",
    "check_runtime",
    "check_harnesses",
    "check_brief",
    "check_health",
    "check_fleet",
    "check_eval",
    "check_registry_notes",
    "check_mcp_clients",
    "check_docs_health",
    "check_agent_docs",
    "check_agent_models",
    "check_agent_perms",
    "check_lint",
    "check_tests",
}


class GateTest(unittest.TestCase):
    def test_check_list_is_complete_and_unique(self):
        names = [fn.__name__ for fn in gate.CHECKS]
        self.assertEqual(set(names), EXPECTED_CHECKS)
        self.assertEqual(len(names), len(set(names)), "a check is registered twice")

    def test_every_check_is_callable(self):
        for fn in gate.CHECKS:
            self.assertTrue(callable(fn), fn)

    def test_check_shim_reexports_the_gate(self):
        self.assertIs(check.main, gate.main)
        self.assertIs(check.CHECKS, gate.CHECKS)


if __name__ == "__main__":
    unittest.main()
