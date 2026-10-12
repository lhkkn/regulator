"""Unit tests for conductor/actuator.py — the precondition→act→verify→record envelope."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conductor import actuator, ledger  # noqa: E402
from conductor.actuator import Contract  # noqa: E402
from tests.support import EnvTestCase  # noqa: E402


class Fake:
    def __init__(self):
        self.acted = 0
        self.rolled = 0


def make(fake, *, pre=True, verify=True, reversible=True, raised=False, effect=None):
    def precondition(args):
        return pre, "pre ok" if pre else "not warranted"

    def act(args):
        fake.acted += 1
        if raised:
            raise RuntimeError("boom")
        return effect if effect is not None else {"changed": True}

    def check(args, eff):
        return verify, "verify ok" if verify else "verify failed"

    def rollback(args, eff):
        fake.rolled += 1

    return Contract(
        name="demo.run",
        tool="conductor.demo.run",
        description="demo",
        precondition=precondition,
        act=act,
        verify=check,
        rollback=rollback,
        reversible=reversible,
    )


class ActuatorTest(EnvTestCase):
    def setUp(self):
        super().setUp()
        self.set_env(CONDUCTOR_APP_DIR=self.tmp.name)

    def test_dry_run_does_not_act_or_ledger(self):
        f = Fake()
        result = actuator.run(make(f), {}, apply=False)
        self.assertEqual(result["outcome"], "dry-run")
        self.assertEqual(f.acted, 0)
        self.assertEqual(ledger.load(), [])
        self.assertIn("plan", result)

    def test_precondition_refusal(self):
        f = Fake()
        result = actuator.run(make(f, pre=False), {}, apply=False)
        self.assertEqual(result["outcome"], "refused")
        self.assertFalse(result["ok"])
        self.assertEqual(ledger.load(), [])
        actuator.run(make(f, pre=False), {}, apply=True)
        self.assertEqual(ledger.load()[0]["outcome"], "refused")

    def test_apply_success_is_verified_and_recorded(self):
        f = Fake()
        result = actuator.run(make(f), {"project": "x"}, apply=True)
        self.assertEqual(result["outcome"], "verified")
        self.assertTrue(result["ok"])
        self.assertEqual(f.acted, 1)
        entries = ledger.load()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["outcome"], "verified")
        self.assertEqual(entries[0]["args"]["project"], "x")

    def test_verify_failure_rolls_back(self):
        f = Fake()
        result = actuator.run(make(f, verify=False), {}, apply=True)
        self.assertEqual(result["outcome"], "rolled_back")
        self.assertFalse(result["ok"])
        self.assertEqual(f.rolled, 1)
        self.assertEqual(ledger.load()[0]["outcome"], "rolled_back")

    def test_verify_failure_irreversible_flags(self):
        f = Fake()
        result = actuator.run(make(f, verify=False, reversible=False), {}, apply=True)
        self.assertEqual(result["outcome"], "flagged")
        self.assertEqual(f.rolled, 0)

    def test_act_exception_is_failed(self):
        f = Fake()
        result = actuator.run(make(f, raised=True), {}, apply=True)
        self.assertEqual(result["outcome"], "failed")
        self.assertIn("boom", result["effect"]["error"])
        self.assertEqual(ledger.load()[0]["outcome"], "failed")

    def test_private_effect_never_leaves_or_is_ledgered(self):
        f = Fake()
        result = actuator.run(make(f, effect={"_prior": "SECRET", "public": 1}), {}, apply=True)
        self.assertNotIn("_prior", result["effect"])
        self.assertNotIn("_prior", ledger.load()[0]["effect"])
        self.assertIn("public", result["effect"])

    def test_render_never_raises(self):
        f = Fake()
        self.assertIsInstance(actuator.render(actuator.run(make(f), {})), str)


if __name__ == "__main__":
    unittest.main()
