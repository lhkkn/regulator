"""Unit tests for conductor/crps.py — the Consultation Request Pack store and its contract.

Boundary stubbing only: the SQLite file and the ledger state dir are redirected to a temp
dir. Nothing about the store or the envelope is mocked — these exercise the real schema,
the real guards, and the real actuator path.
"""

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conductor import actuator, contracts, crps  # noqa: E402
from tests.support import EnvTestCase  # noqa: E402


class CrpStoreTest(EnvTestCase):
    """file → queue → answer, plus the guards that keep the queue honest."""

    def setUp(self):
        super().setUp()
        self.set_env(CONDUCTOR_CRP_DB=str(self.tmpdir / "crp.db"))
        self.set_env(CONDUCTOR_APP_DIR=str(self.tmpdir / "state"))

    def test_files_an_open_crp(self):
        cid = crps.file(
            "split finance from tasks?",
            options=["split them", "leave it"],
            context="one secret, two consumers",
            blast_radius="reversible",
        )
        row = crps.get(cid)
        self.assertEqual(row["status"], crps.OPEN)
        self.assertEqual(row["question"], "split finance from tasks?")
        self.assertEqual([o["label"] for o in json.loads(row["options"])], ["split them", "leave it"])

    def test_options_accept_label_tradeoff_dicts(self):
        cid = crps.file("which?", options=[{"label": "a", "tradeoff": "cheap"}, {"label": "b"}])
        opts = json.loads(crps.get(cid)["options"])
        self.assertEqual(opts[0], {"label": "a", "tradeoff": "cheap"})
        self.assertEqual(opts[1], {"label": "b", "tradeoff": ""})

    def test_open_queue_is_oldest_first(self):
        """A queue is answered in order — a stack would starve the oldest question."""
        a = crps.file("first?")
        b = crps.file("second?")
        self.assertEqual([r["id"] for r in crps.open_crps()], [a, b])

    def test_answer_removes_it_from_the_queue(self):
        cid = crps.file("which?")
        self.assertTrue(crps.answer(cid, "this one", "obvious in hindsight"))
        self.assertEqual(crps.open_crps(), [])
        self.assertIn("this one", crps.get(cid)["answer"])
        self.assertIn("obvious", crps.get(cid)["answer"])

    def test_answering_twice_is_refused(self):
        """A resolved CRP stays resolved — the answer is not silently overwritable."""
        cid = crps.file("which?")
        self.assertTrue(crps.answer(cid, "a"))
        self.assertFalse(crps.answer(cid, "b"))

    def test_answering_a_missing_crp_is_refused(self):
        self.assertFalse(crps.answer(999, "a"))

    def test_withdraw_is_the_rollback(self):
        cid = crps.file("never mind?")
        self.assertTrue(crps.withdraw(cid))
        self.assertEqual(crps.get(cid)["status"], crps.WITHDRAWN)
        self.assertEqual(crps.open_crps(), [])

    def test_duplicate_open_question_is_detected(self):
        crps.file("same question?")
        self.assertTrue(crps.is_duplicate_open("  SAME QUESTION?  "))

    def test_duplicate_guard_relaxes_once_answered(self):
        """Otherwise a resolved question could never be asked again."""
        cid = crps.file("same question?")
        crps.answer(cid, "done")
        self.assertFalse(crps.is_duplicate_open("same question?"))

    def test_default_after_rejects_a_past_deadline(self):
        """A past deadline would fire the default immediately — refuse it at the door."""
        ok, why = crps.parse_default_after("2020-01-01")
        self.assertFalse(ok)
        self.assertIn("past", why)

    def test_default_after_rejects_nonsense(self):
        ok, _ = crps.parse_default_after("next tuesday")
        self.assertFalse(ok)

    def test_default_after_accepts_a_future_deadline(self):
        ok, _ = crps.parse_default_after("2030-01-01")
        self.assertTrue(ok)

    def test_summary_reports_the_open_count(self):
        crps.file("one?")
        crps.file("two?")
        s = crps.summary()
        self.assertEqual(s["open"], 2)
        self.assertEqual(len(s["rows"]), 2)

    def test_render_survives_a_corrupt_options_blob(self):
        """The store must not be able to crash the digest by holding bad JSON."""
        cid = crps.file("which?")
        with crps._conn() as c:  # noqa: SLF001 - deliberately corrupting our own row
            c.execute("UPDATE crps SET options = ? WHERE id = ?", ("{not json", cid))
        self.assertIn("which?", crps.render(crps.open_crps()))


class CrpContractTest(EnvTestCase):
    """The same store, driven through the real actuator envelope."""

    def setUp(self):
        super().setUp()
        self.set_env(CONDUCTOR_CRP_DB=str(self.tmpdir / "crp.db"))
        self.set_env(CONDUCTOR_APP_DIR=str(self.tmpdir / "state"))

    def test_dry_run_plans_and_writes_nothing(self):
        con = contracts.get("crp_file")
        res = actuator.run(con, {"question": "should I?"}, apply=False)
        self.assertEqual(res["outcome"], "dry-run")
        self.assertIn("would run", res["plan"])
        self.assertEqual(crps.open_crps(), [])

    def test_apply_files_and_verifies(self):
        con = contracts.get("crp_file")
        res = actuator.run(con, {"question": "should I?", "default_action": "proceed"}, apply=True)
        self.assertEqual(res["outcome"], "verified", res)
        self.assertTrue(res["verify"]["ok"])
        self.assertEqual(len(crps.open_crps()), 1)

    def test_precondition_refuses_a_duplicate(self):
        con = contracts.get("crp_file")
        actuator.run(con, {"question": "should I?"}, apply=True)
        res = actuator.run(con, {"question": "should I?"}, apply=True)
        self.assertEqual(res["outcome"], "refused")
        self.assertEqual(len(crps.open_crps()), 1)

    def test_precondition_refuses_an_empty_question(self):
        res = actuator.run(contracts.get("crp_file"), {"question": "   "}, apply=True)
        self.assertEqual(res["outcome"], "refused")

    def test_precondition_refuses_a_past_default(self):
        res = actuator.run(
            contracts.get("crp_file"),
            {"question": "should I?", "default_after": "2020-01-01"},
            apply=True,
        )
        self.assertEqual(res["outcome"], "refused")

    def test_rollback_withdraws_the_crp(self):
        con = contracts.get("crp_file")
        res = actuator.run(con, {"question": "should I?"}, apply=True)
        con.rollback({}, res["effect"])
        self.assertEqual(crps.open_crps(), [])

    def test_contract_is_reversible_with_a_rollback(self):
        con = contracts.get("crp_file")
        self.assertTrue(con.reversible)
        self.assertIsNotNone(con.rollback)
        self.assertEqual(con.risk, "write")


class CrpSurfaceTest(unittest.TestCase):
    """The design principle, asserted: filing is exposed, answering is not."""

    def test_crp_file_is_a_governed_write_tool(self):
        from conductor.mcp.tools import TOOLS

        tools = {t["name"]: t for t in TOOLS}
        self.assertIn("crp_file", tools)
        self.assertEqual(tools["crp_file"]["risk"], "write")
        self.assertIsNotNone(contracts.get("crp_file"))

    def test_no_agent_can_answer_its_own_question(self):
        """An MCP `crp_answer` would defeat the entire mechanism.

        The asymmetry is what makes a CRP a *handoff* rather than another tool the agent
        can use to talk itself into things. This test exists to make removing that
        property a deliberate, visible act.
        """
        from conductor.mcp.tools import TOOLS

        names = {t["name"] for t in TOOLS}
        self.assertIn("crp_file", names)
        self.assertNotIn("crp_answer", names)
        self.assertNotIn("crp_resolve", names)


class CrpExpiryTest(EnvTestCase):
    """A lapsed deadline is recorded and surfaced — never silently executed."""

    def setUp(self):
        super().setUp()
        self.set_env(CONDUCTOR_CRP_DB=str(self.tmpdir / "crp.db"))
        self.set_env(CONDUCTOR_APP_DIR=str(self.tmpdir / "state"))

    def _lapsed(self, question: str = "which?", **kw) -> int:
        """A CRP whose deadline has passed — filed ahead of time, then rewound.

        `file()` refuses a past deadline by design, so a lapsed CRP is built the way one
        actually arises: filed with a future deadline, then time passes. Reaching behind
        the guard here is a fixture, not a bypass — the rule is asserted separately.
        """
        cid = crps.file(question, default_after="2030-01-01", **kw)
        with crps._conn() as c:  # noqa: SLF001 - constructing the state under test
            c.execute("UPDATE crps SET default_after = ? WHERE id = ?", ("2020-01-01", cid))
        return cid

    def test_expire_due_marks_a_lapsed_crp(self):
        cid = self._lapsed(default_action="proceed anyway")
        gone = crps.expire_due()
        self.assertEqual([r["id"] for r in gone], [cid])
        self.assertEqual(crps.get(cid)["status"], crps.EXPIRED)
        self.assertEqual(crps.open_crps(), [])

    def test_expire_due_leaves_a_future_deadline_alone(self):
        crps.file("which?", default_after="2030-01-01")
        self.assertEqual(crps.expire_due(), [])
        self.assertEqual(len(crps.open_crps()), 1)

    def test_a_crp_without_a_deadline_never_expires(self):
        """No declared deadline means no default — it just waits."""
        crps.file("which?")
        self.assertEqual(crps.expire_due(), [])
        self.assertEqual(len(crps.open_crps()), 1)

    def test_lapsed_lists_them_for_the_digest(self):
        self._lapsed()
        crps.expire_due()
        self.assertEqual(len(crps.lapsed()), 1)

    def test_expiry_records_but_does_not_run_the_default(self):
        """The design boundary: conductor records that a deadline lapsed.

        Executing an arbitrary declared action unattended is the authority the human side
        exists to withhold. The agent resumes and acts on its own default, with the fact
        on the record — conductor does not act for it.
        """
        self._lapsed(default_action="DELETE EVERYTHING")
        crps.expire_due()
        row = crps.lapsed()[0]
        self.assertEqual(row["default_action"], "DELETE EVERYTHING")  # preserved as text
        self.assertEqual(row["status"], crps.EXPIRED)  # and nothing else happened

    def test_expiring_an_answered_crp_is_impossible(self):
        cid = self._lapsed()
        crps.answer(cid, "answered first")
        self.assertEqual(crps.expire_due(), [])
        self.assertEqual(crps.get(cid)["status"], crps.ANSWERED)

    def test_summary_expires_as_a_side_effect(self):
        """brief must not depend on the watcher having run."""
        self._lapsed()
        s = crps.summary()
        self.assertEqual(s["open"], 0)
        self.assertEqual(s["lapsed"], 1)


class CrpQuotaTest(EnvTestCase):
    """A confused run must not be able to bury the queue."""

    def setUp(self):
        super().setUp()
        self.set_env(CONDUCTOR_CRP_DB=str(self.tmpdir / "crp.db"))
        self.set_env(CONDUCTOR_APP_DIR=str(self.tmpdir / "state"))

    def test_open_count_scopes_to_the_actor(self):
        crps.file("a?", actor="agent-one")
        crps.file("b?", actor="agent-two")
        self.assertEqual(crps.open_count("agent-one"), 1)
        self.assertEqual(crps.open_count("agent-two"), 1)
        self.assertEqual(crps.open_count(), 2)

    def test_the_contract_refuses_past_the_quota(self):
        con = contracts.get("crp_file")
        for i in range(crps.MAX_OPEN_PER_ACTOR):
            res = actuator.run(con, {"question": f"q{i}?", "actor": "greedy"}, apply=True)
            self.assertEqual(res["outcome"], "verified", res)
        res = actuator.run(con, {"question": "one too many?", "actor": "greedy"}, apply=True)
        self.assertEqual(res["outcome"], "refused")
        self.assertIn(f"already has {crps.MAX_OPEN_PER_ACTOR} open CRPs", res["precondition"]["detail"])
        self.assertEqual(crps.open_count("greedy"), crps.MAX_OPEN_PER_ACTOR)

    def test_one_actors_quota_does_not_block_another(self):
        con = contracts.get("crp_file")
        for i in range(crps.MAX_OPEN_PER_ACTOR):
            actuator.run(con, {"question": f"q{i}?", "actor": "greedy"}, apply=True)
        res = actuator.run(con, {"question": "mine?", "actor": "polite"}, apply=True)
        self.assertEqual(res["outcome"], "verified", res)


class CrpOneRuleTest(EnvTestCase):
    """The guards must hold on *every* door.

    The agentloop chore agent reaches CRPs through the CLI (`python3 -m conductor *` is on
    its bash allowlist), so a guard living only in the actuator contract would bound the
    MCP path and leave the unattended one wide open. These tests exist because that was
    true until it wasn't.
    """

    def setUp(self):
        super().setUp()
        self.set_env(CONDUCTOR_CRP_DB=str(self.tmpdir / "crp.db"))
        self.set_env(CONDUCTOR_APP_DIR=str(self.tmpdir / "state"))

    def _cli(self, argv: list[str]) -> int:
        """Run the CLI with output captured — the gate log should stay readable."""
        with contextlib.redirect_stdout(io.StringIO()):
            return crps.main(argv)

    def test_the_store_refuses_a_duplicate(self):
        crps.file("same?")
        with self.assertRaises(crps.Refused):
            crps.file("same?")

    def test_the_store_refuses_past_the_quota(self):
        for i in range(crps.MAX_OPEN_PER_ACTOR):
            crps.file(f"q{i}?", actor="loop")
        with self.assertRaises(crps.Refused):
            crps.file("one more?", actor="loop")

    def test_the_store_refuses_a_past_deadline(self):
        with self.assertRaises(crps.Refused):
            crps.file("which?", default_after="2020-01-01")

    def test_the_store_refuses_an_empty_question(self):
        with self.assertRaises(crps.Refused):
            crps.file("   ")

    def test_the_cli_refuses_rather_than_tracebacking(self):
        crps.file("same?")
        rc = self._cli(["file", "same?"])
        self.assertEqual(rc, 2)

    def test_the_cli_files_when_the_rule_allows(self):
        self.assertEqual(self._cli(["file", "a fresh question?"]), 0)
        self.assertEqual(crps.open_count(), 1)

    def test_the_cli_honours_the_actor_env_var(self):
        """An agent identifies itself so its quota is its own."""
        self.set_env(CONDUCTOR_ACTOR="agentloop:drift")
        self._cli(["file", "from the loop?"])
        self.assertEqual(crps.open_count("agentloop:drift"), 1)
        self.assertEqual(crps.open_count("cli"), 0)

    def test_both_paths_agree_on_duplicates(self):
        """Same rule, same verdict — the property the whole class is about."""
        crps.file("same?")
        con = contracts.get("crp_file")
        via_contract = actuator.run(con, {"question": "same?"}, apply=True)
        try:
            crps.file("same?")
            via_cli = None
        except crps.Refused:
            via_cli = "refused"
        self.assertEqual(via_contract["outcome"], "refused")
        self.assertEqual(via_cli, "refused")

    def test_both_paths_agree_on_the_quota(self):
        con = contracts.get("crp_file")
        for i in range(crps.MAX_OPEN_PER_ACTOR):
            actuator.run(con, {"question": f"q{i}?", "actor": "loop"}, apply=True)
        via_contract = actuator.run(con, {"question": "more?", "actor": "loop"}, apply=True)
        try:
            crps.file("more?", actor="loop")
            via_cli = None
        except crps.Refused:
            via_cli = "refused"
        self.assertEqual(via_contract["outcome"], "refused")
        self.assertEqual(via_cli, "refused")
