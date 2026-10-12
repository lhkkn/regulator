"""Unit tests for conductor/flows.py — the declarative workflow graph (pure logic)."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conductor import flows  # noqa: E402


class GraphTest(unittest.TestCase):
    def test_topological_order_respects_needs(self):
        steps = [
            {"id": "c", "action": "brief", "needs": ["a", "b"]},
            {"id": "a", "action": "stack"},
            {"id": "b", "action": "stack"},
        ]
        ids = [s["id"] for s in flows._order(steps)]
        self.assertLess(ids.index("a"), ids.index("c"))
        self.assertLess(ids.index("b"), ids.index("c"))

    def test_when_conditions(self):
        res = {"a": {"ok": True}, "b": {"ok": False}}
        self.assertTrue(flows._when_ok("always", res))
        self.assertTrue(flows._when_ok("any_fail", res))
        self.assertFalse(flows._when_ok("all_ok", res))
        self.assertTrue(flows._when_ok("a_ok", res))
        self.assertTrue(flows._when_ok("b_fail", res))
        self.assertFalse(flows._when_ok("a_fail", res))


class BuiltinsTest(unittest.TestCase):
    def test_audit_flow_exists_and_lists(self):
        names = [f["name"] for f in flows.list_flows()]
        self.assertIn("audit", names)
        self.assertIn("start", names)
        self.assertIn("flows:", flows.render_list())

    def test_graph_renders(self):
        g = flows.graph("audit")
        self.assertIn("graph TD", g)
        self.assertIn("drift", g)

    def test_audit_dry_run_plan(self):
        result = flows.run("audit", dry=True)
        self.assertTrue(result["dry"])
        self.assertEqual(len(result["plan"]), 6)

    def test_fix_drift_flow_exists_with_write_steps(self):
        names = [f["name"] for f in flows.list_flows()]
        self.assertIn("fix-drift", names)
        plan = flows.run("fix-drift", dry=True)
        actions = {s["action"] for s in plan["plan"]}
        self.assertIn("reconcile", actions)
        self.assertIn("projects.register-drift", actions)
        self.assertIn("docker.prune", actions)
        self.assertTrue(any(s["write"] for s in plan["plan"]))

    def test_unknown_flow(self):
        self.assertIn("error", flows.run("no-such-flow"))

    def test_run_with_stubbed_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            flowdir = Path(tmp)
            (flowdir / "x.json").write_text(
                '{"name":"x","steps":[{"id":"a","action":"stack"},'
                '{"id":"b","action":"brief","needs":["a"],"when":"a_ok"}]}'
            )
            with mock.patch.object(flows, "FLOWS_DIR", flowdir):
                with mock.patch.dict(flows.ACTIONS, {"stack": lambda: (True, "s"), "brief": lambda: (True, "b")}):
                    result = flows.run("x")
        self.assertTrue(result["results"]["b"]["ok"])


class WriteStepTest(unittest.TestCase):
    def _flow(self, tmp):
        flowdir = Path(tmp)
        (flowdir / "w.json").write_text('{"name":"w","steps":[{"id":"a","action":"backup.run","args":{"force":true}}]}')
        return flowdir

    def test_write_step_defaults_to_dry_run(self):
        dry = {"outcome": "dry-run", "ok": True, "apply": False, "precondition": {"ok": True, "detail": "due"}}
        with tempfile.TemporaryDirectory() as tmp:
            flowdir = self._flow(tmp)
            with (
                mock.patch.object(flows, "FLOWS_DIR", flowdir),
                mock.patch("conductor.contracts.execute", return_value=dry) as ex,
            ):
                result = flows.run("w")
        step = result["results"]["a"]
        self.assertTrue(step["write"])
        self.assertFalse(step["applied"])
        self.assertEqual(ex.call_args[0][1]["apply"], False)

    def test_write_apply_refused_without_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            flowdir = self._flow(tmp)
            with (
                mock.patch.object(flows, "FLOWS_DIR", flowdir),
                mock.patch("conductor.permissions.check", return_value="denied by policy"),
            ):
                result = flows.run("w", apply=True)
        step = result["results"]["a"]
        self.assertFalse(step["ok"])
        self.assertIn("refused by policy", step["text"])

    def test_write_apply_runs_when_allowed(self):
        ok = {
            "outcome": "verified",
            "ok": True,
            "apply": True,
            "effect": {"summary": "done"},
            "precondition": {"ok": True, "detail": "due"},
        }
        with tempfile.TemporaryDirectory() as tmp:
            flowdir = self._flow(tmp)
            with (
                mock.patch.object(flows, "FLOWS_DIR", flowdir),
                mock.patch("conductor.permissions.check", return_value=None),
                mock.patch("conductor.contracts.execute", return_value=ok),
            ):
                result = flows.run("w", apply=True)
        step = result["results"]["a"]
        self.assertTrue(step["applied"])
        self.assertTrue(step["ok"])

    def test_write_not_due_is_skipped_not_failed(self):
        refused = {
            "outcome": "refused",
            "ok": False,
            "apply": False,
            "precondition": {"ok": False, "detail": "nothing to do"},
        }
        with tempfile.TemporaryDirectory() as tmp:
            flowdir = self._flow(tmp)
            with (
                mock.patch.object(flows, "FLOWS_DIR", flowdir),
                mock.patch("conductor.contracts.execute", return_value=refused),
            ):
                result = flows.run("w")
        step = result["results"]["a"]
        self.assertTrue(step["ok"])
        self.assertTrue(step["skipped"])
        self.assertIn("skipped", step["text"])


if __name__ == "__main__":
    unittest.main()
