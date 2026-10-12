"""Unit tests for conductor/permissions.py — risk classes + the enforced policy gate.

Tool names are *derived* from the registry, never written as literals. A literal here
would compare against the same string the implementation uses, so the assertion would
mirror the code: renaming a tool would keep the test green while the gate silently
stopped applying. That is not hypothetical — it is the bug the regression seed at the
bottom of this file exists to prevent.
"""

import contextlib
import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conductor import permissions  # noqa: E402
from conductor.mcp.tools import TOOLS  # noqa: E402
from tests.support import EnvTestCase  # noqa: E402


def _quiet(argv):
    """Run the policy CLI without its render output polluting the test log."""
    with contextlib.redirect_stdout(io.StringIO()):
        permissions.main(argv)


def _tool(handler_name: str) -> str:
    """A tool's registered name, found by its handler — survives a rename."""
    return next(t["name"] for t in TOOLS if t["handler"].__name__ == handler_name)


class PermissionsTest(EnvTestCase):
    def setUp(self):
        super().setUp()
        self.set_env(CONDUCTOR_APP_DIR=self.tmp.name)

    def test_read_is_allowed(self):
        self.assertIsNone(permissions.check(_tool("brief"), {}))

    def test_reveal_denied_by_default(self):
        self.assertIsNotNone(permissions.check(_tool("keys_lookup"), {"reveal": True}))

    def test_reveal_allowed_after_opt_in(self):
        _quiet(["--allow-reveal"])
        self.assertIsNone(permissions.check(_tool("keys_lookup"), {"reveal": True}))

    def test_masked_lookup_is_allowed(self):
        self.assertIsNone(permissions.check(_tool("keys_lookup"), {}))

    def test_unknown_tool_is_write_and_denied(self):
        self.assertIn("write", permissions.check("no.such.tool", {}))

    def test_write_tool_dry_run_is_allowed(self):
        self.assertIsNone(permissions.check(_tool("backup_run"), {"apply": False}))
        self.assertIsNone(permissions.check(_tool("backup_run"), {}))

    def test_write_tool_apply_is_denied_by_default(self):
        denial = permissions.check(_tool("backup_run"), {"apply": True})
        self.assertIsNotNone(denial)
        self.assertIn("--allow-write", denial)

    def test_write_tool_apply_allowed_after_opt_in(self):
        _quiet(["--allow-write", _tool("backup_run")])
        self.assertIsNone(permissions.check(_tool("backup_run"), {"apply": True}))

    def test_read_only_mode_denies_sensitive(self):
        _quiet(["--read-only"])
        self.assertIsNotNone(permissions.check(_tool("keys_lookup"), {}))

    def test_explicit_deny_wins(self):
        _quiet(["--deny", _tool("brief")])
        self.assertIsNotNone(permissions.check(_tool("brief"), {}))

    def test_risk_map_covers_every_tool(self):
        self.assertEqual(set(permissions.risk_map()), {t["name"] for t in TOOLS})

    def test_every_tool_has_a_valid_risk(self):
        for t in TOOLS:
            self.assertIn(t.get("risk"), ("read", "sensitive", "write"), t.get("name"))

    # --- gated arguments (the reveal rule) -------------------------------------------

    def test_gated_arg_is_declared_in_the_registry(self):
        """The gate must be declared data, not a name comparison in the policy layer."""
        self.assertEqual(
            permissions.gated_args(_tool("keys_lookup")),
            {
                "reveal": {"flag": "allow_reveal", "why": "reaches 1Password and triggers Touch ID"},
            },
        )

    def test_gate_follows_a_renamed_tool(self):
        """# regression: renaming keys.lookup silently opened the reveal gate.

        The rule used to be `if tool == "conductor.keys.lookup"`. Renaming the tool left
        the literal behind, so `reveal=true` fell through the sensitive branch to
        `return None` — allowed — while this suite stayed green, because the old test
        passed the same literal the implementation compared against.
        """
        entry = next(t for t in TOOLS if t["handler"].__name__ == "keys_lookup")
        original = entry["name"]
        renamed = original + ".renamed"
        try:
            entry["name"] = renamed
            self.assertIsNotNone(
                permissions.check(renamed, {"reveal": True}),
                "the reveal gate did not follow the renamed tool",
            )
        finally:
            entry["name"] = original

    # --- stale policy names -----------------------------------------------------------

    def test_stale_policy_name_is_reported(self):
        """A rule naming a tool that does not exist is a silent no-op unless reported."""
        _quiet(["--deny", "conductor.no.such.tool"])
        self.assertEqual(permissions.unknown_policy_names(), ["conductor.no.such.tool"])

    def test_valid_policy_names_are_not_reported(self):
        _quiet(["--deny", _tool("brief")])
        self.assertEqual(permissions.unknown_policy_names(), [])

    def test_render_flags_a_stale_name(self):
        _quiet(["--deny", "conductor.no.such.tool"])
        self.assertIn("STALE", permissions.render())


if __name__ == "__main__":
    unittest.main()
