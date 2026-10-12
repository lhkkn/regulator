"""Unit tests for conductor/brief.py — the digest composition.

build_brief() calls the live collectors; tests stub every private helper so the
composition (text structure + flags) is asserted deterministically, with no
workspace reads or network. Adding a collector means adding it to `_stub`, or the
test silently starts reading real state.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conductor import brief  # noqa: E402

# A realistically-shaped open CRP row, so the stub exercises the real renderer.
_CRP_ROW = {
    "id": 1,
    "ts": "2026-01-01 00:00",
    "actor": "agent",
    "project": "demo",
    "question": "Which way?",
    "options": '[{"label": "a", "tradeoff": "cheap"}, {"label": "b", "tradeoff": "safe"}]',
    "context": "",
    "blast_radius": "",
    "default_action": "",
    "default_after": "",
    "status": "open",
    "answer": None,
}


class BriefTest(unittest.TestCase):
    def _patch(self, obj, **attrs):
        """Replace module attributes, restoring the originals via addCleanup.

        Without this the stubs leak into every later test in the session (build_brief's
        collectors and docs_health are module globals).
        """
        for name, value in attrs.items():
            self.addCleanup(setattr, obj, name, getattr(obj, name))
            setattr(obj, name, value)

    def _stub(
        self,
        *,
        spend_usd=0.0,
        drift_code=0,
        installed=("opencode", "kimi"),
        failing_checks=(),
        failure_counts=None,
        health_flags=(),
        crp_open=0,
        crp_lapsed=0,
    ):
        checks = {
            name: (name not in failing_checks, f"{name}: {'FAIL' if name in failing_checks else 'ok'}")
            for name in ("backup", "server_backup", "drill", "keys", "disk", "companies", "crp")
        }
        self._patch(
            brief,
            _failure_counts=lambda: dict(failure_counts or {}),
            _spend=lambda: {"usd": spend_usd, "by_source": {"kimi": {"usd": spend_usd, "calls": 3}}},
            _drift=lambda: (drift_code, "output"),
            _agent_stack=lambda: {"installed": list(installed), "missing": ["claude", "ollama"]},
            _balances=lambda: {"records": [], "summary": {}},
            _health_checks=lambda live: checks,
            _health=lambda live: (list(health_flags), "- probe ok"),
            _watch=lambda: {"alive": True, "age_hours": 0.5, "updated": "now"},
            # Without this the collector reads the *real* workspace `.crp.db`, so a live
            # open question would fail test_all_clear. Every collector is stubbed here;
            # a new one must be added to this list, not just to build_brief.
            _crps=lambda: {
                "open": crp_open,
                "rows": [_CRP_ROW] * crp_open,
                "lapsed": crp_lapsed,
                "lapsed_rows": [{**_CRP_ROW, "status": "expired"}] * crp_lapsed,
            },
        )
        # docs_health is imported inside build_brief() — neutralize it so the test
        # asserts composition, not the live workspace's doc tree.
        from conductor import docs_health

        self._patch(docs_health, report=lambda: [], render=lambda rows: ([], []))

    def test_all_clear(self):
        self._stub()
        b = brief.build_brief(live_health=False)
        self.assertIn("# Regulator brief", b["text"])
        self.assertEqual(b["flags"], [])
        self.assertIn("all clear", b["text"])

    def test_high_spend_flag(self):
        self._stub(spend_usd=5.0)
        b = brief.build_brief(live_health=False)
        self.assertTrue(any("high spend" in f for f in b["flags"]))

    def test_drift_flag(self):
        self._stub(drift_code=1)
        b = brief.build_brief(live_health=False)
        self.assertTrue(any("drift" in f for f in b["flags"]))
        self.assertIn("## Workspace drift", b["text"])

    def test_a_failing_check_becomes_a_flag_and_renders(self):
        """The digest reads the watcher's verdict — it never forms its own opinion.

        This is the regression guard for the 36h/48h divergence: `brief` used to apply its
        own thresholds to backup and drill, so it could call a snapshot stale while the
        watcher called it fine. Now a failing check is a failing line, once.
        """
        self._stub(failing_checks=("keys",))
        b = brief.build_brief(live_health=False)
        self.assertTrue(any(f == "keys: FAIL" for f in b["flags"]))
        self.assertIn("## Health", b["text"])
        self.assertIn("FAIL keys: FAIL", b["text"])
        self.assertNotIn("## Keys", b["text"])  # the old, separately-derived section is gone

    def test_all_checks_render_in_one_section(self):
        self._stub()
        b = brief.build_brief(live_health=False)
        for name in ("backup", "server_backup", "drill", "keys", "disk", "companies", "crp"):
            self.assertIn(name, b["text"])

    def test_a_new_failure_is_flagged(self):
        self._stub(failing_checks=("disk",), failure_counts={"disk": 2})
        b = brief.build_brief(live_health=False)
        self.assertTrue(any(f == "disk: FAIL" for f in b["flags"]))
        self.assertIn("FAIL disk: FAIL", b["text"])
        self.assertEqual(b["summary"]["chronic_checks"], [])

    def test_a_chronic_failure_stops_being_news_but_is_still_reported(self):
        """189 consecutive FAILs is a check nobody reads.

        The finding stays visible — it is true, and hiding it would be worse — but it
        stops raising a ⚠, because a permanently red signal trains the skim that then
        gets applied to the failures that just started.
        """
        self._stub(failing_checks=("companies",), failure_counts={"companies": 189})
        b = brief.build_brief(live_health=False)
        self.assertFalse(any("companies" in f for f in b["flags"]))
        self.assertIn("old  companies: FAIL", b["text"])  # reported, marked, not shouted
        self.assertEqual(b["summary"]["failing_checks"], ["companies"])
        self.assertEqual(b["summary"]["chronic_checks"], ["companies"])

    def test_the_boundary_is_config_not_a_magic_number(self):
        from conductor import config

        just_under = config.CHRONIC_AFTER_RUNS - 1
        self._stub(failing_checks=("disk",), failure_counts={"disk": just_under})
        self.assertTrue(any("disk" in f for f in brief.build_brief(live_health=False)["flags"]))

        self._stub(failing_checks=("disk",), failure_counts={"disk": config.CHRONIC_AFTER_RUNS})
        self.assertFalse(any("disk" in f for f in brief.build_brief(live_health=False)["flags"]))

    def test_missing_core_agents_flag(self):
        self._stub(installed=("ollama",))
        b = brief.build_brief(live_health=False)
        self.assertTrue(any("no core agent CLI" in f for f in b["flags"]))

    def test_health_flags_extend(self):
        self._stub(health_flags=("[health] kimi: DEAD",))
        b = brief.build_brief(live_health=False)
        self.assertTrue(any("[health]" in f for f in b["flags"]))

    def test_open_crp_flag_and_section(self):
        """A CRP is the only digest item waiting on a human — it must be flagged and
        rendered above the state sections."""
        self._stub(crp_open=2)
        b = brief.build_brief(live_health=False)
        self.assertTrue(any("open CRP" in f for f in b["flags"]))
        self.assertIn("Waiting on you", b["text"])
        # above Flags: a queue with someone blocked behind it outranks state to be aware of
        self.assertLess(b["text"].index("Waiting on you"), b["text"].index("## Flags"))
        self.assertEqual(b["summary"]["crp_open"], 2)

    def test_no_crp_section_when_the_queue_is_empty(self):
        self._stub(crp_open=0)
        b = brief.build_brief(live_health=False)
        self.assertNotIn("Waiting on you", b["text"])
        self.assertFalse(any("CRP" in f for f in b["flags"]))

    def test_lapsed_crp_is_flagged_and_rendered_first(self):
        """A fired default outranks an open question: it is already happening."""
        self._stub(crp_open=1, crp_lapsed=1)
        b = brief.build_brief(live_health=False)
        self.assertTrue(any("default(s) fired" in f for f in b["flags"]))
        self.assertIn("Default now in effect", b["text"])
        self.assertLess(b["text"].index("Default now in effect"), b["text"].index("Waiting on you"))
        self.assertEqual(b["summary"]["crp_lapsed"], 1)

    def test_summary_shape(self):
        self._stub(spend_usd=1.5, drift_code=0, failing_checks=("disk",))
        b = brief.build_brief(live_health=False)
        s = b["summary"]
        self.assertEqual(s["drift"], 0)
        self.assertEqual(s["spend_usd_24h"], 1.5)
        self.assertEqual(s["failing_checks"], ["disk"])
        self.assertEqual(s["checks"]["backup"]["ok"], True)
        self.assertEqual(s["agents_installed"], ["opencode", "kimi"])
        self.assertIsInstance(s["flags"], list)


class SpendFlagThresholdTest(unittest.TestCase):
    def test_threshold_lives_in_config(self):
        from conductor import config

        self.assertEqual(config.SPEND_FLAG_USD, 2.0)


if __name__ == "__main__":
    unittest.main()
