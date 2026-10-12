"""Unit tests for conductor/tokens/balances.py — per-key provider balances.

The module reveals keys from 1Password and hits provider APIs; tests monkeypatch both
so they run offline. Defensiveness contract: a missing registry, a failed reveal, or a
failed query degrades to error records — never raises.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conductor.tokens import balances  # noqa: E402


class BalancesTest(unittest.TestCase):
    def setUp(self):
        balances._MEMO = None
        self.tmp = tempfile.TemporaryDirectory()
        self.reg = Path(self.tmp.name) / "registry.json"
        self._real_registry_path = balances.registry_path
        self._real_reveal = balances.reveal_value
        self._real_query = balances._query
        self._real_all_balances = balances.all_balances
        balances.registry_path = lambda: self.reg

    def tearDown(self):
        balances.registry_path = self._real_registry_path
        balances.reveal_value = self._real_reveal
        balances._query = self._real_query
        balances.all_balances = self._real_all_balances
        balances._MEMO = None
        self.tmp.cleanup()

    def _write(self, keys):
        self.reg.write_text(json.dumps({"version": 1, "keys": keys}))

    # ------------------------------------------------------------- parsing

    def test_parse_deepseek(self):
        bal, cur, avail = balances._parse_deepseek(
            {"is_available": True, "balance_infos": [{"total_balance": "19.77", "currency": "USD"}]}
        )
        self.assertEqual(bal, 19.77)
        self.assertEqual(cur, "USD")
        self.assertTrue(avail)

    def test_parse_deepseek_empty(self):
        bal, cur, avail = balances._parse_deepseek({"is_available": False, "balance_infos": []})
        self.assertIsNone(bal)
        self.assertFalse(avail)

    # ------------------------------------------------------------- selection

    def test_skips_unsupported_and_unlinked(self):
        self._write(
            [
                {"id": "k1", "provider": "deepseek", "op_ref": "op://v/i/f", "label": "a", "suffix": "s1"},
                {"id": "k2", "provider": "deepseek", "label": "no-link", "suffix": "s2"},  # no op_ref
                {"id": "k3", "provider": "anthropic", "op_ref": "op://v/i/f"},  # unsupported
            ]
        )
        balances.reveal_value = lambda ref: "sk-x"
        balances._query = lambda url, key, parser: {
            "ok": True,
            "balance": 5.0,
            "currency": "USD",
            "is_available": True,
            "error": None,
        }
        self.assertEqual([r["id"] for r in balances.all_balances()], ["k1"])

    def test_no_vault_value_is_error(self):
        self._write([{"id": "k1", "provider": "deepseek", "op_ref": "op://v/i/f", "label": "a", "suffix": "s1"}])
        balances.reveal_value = lambda ref: None
        recs = balances.all_balances()
        self.assertFalse(recs[0]["ok"])
        self.assertEqual(recs[0]["error"], "no vault value")

    # ------------------------------------------------------------- rollup

    def test_summary_dedupes_shared_account(self):
        balances.all_balances = lambda fresh=False: [
            {"id": "k1", "provider": "deepseek", "ok": True, "balance": 19.75, "currency": "USD"},
            {"id": "k2", "provider": "deepseek", "ok": True, "balance": 19.75, "currency": "USD"},
            {"id": "k3", "provider": "deepseek", "ok": False, "balance": None, "currency": None},
        ]
        s = balances.summary()
        self.assertEqual(s["total_balance"], 19.75)  # one account, not 2x
        self.assertEqual(s["accounts"], 1)
        self.assertEqual(s["errors"], 1)

    # ------------------------------------------------------------- defensive

    def test_missing_registry_never_raises(self):
        balances.registry_path = lambda: Path("/nonexistent-registry.json")
        self.assertEqual(balances.all_balances(), [])


if __name__ == "__main__":
    unittest.main()
