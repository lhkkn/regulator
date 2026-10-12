"""Unit tests for conductor/tokens/spend.py — the token-spend parsers.

Parsers read the live home-dir corpora, so tests inject synthetic logs into temp
dirs via monkeypatched module globals and clear the memo cache between tests.
Defensiveness contract: missing dirs / malformed lines degrade, never raise.
"""

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conductor.tokens import spend  # noqa: E402


class SpendTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._patch_paths()

    def tearDown(self):
        spend._MEMO.clear()
        self.tmp.cleanup()

    def _patch_paths(self, kimi=True, claude=True, opencode=True):
        spend.KIMI_SESSIONS = self.root / "kimi" if kimi else Path("/nonexistent-kimi")
        spend.CLAUDE_PROJECTS = self.root / "claude" if claude else Path("/nonexistent-claude")
        spend.OPENCODE_DB = self.root / "opencode.db" if opencode else Path("/nonexistent-opencode.db")

    # ---------------------------------------------------------------- kimi

    def test_kimi_parser(self):
        f = self.root / "kimi" / "wd_1" / "session_1" / "agents" / "main" / "wire.jsonl"
        f.parent.mkdir(parents=True)
        ts_ms = int(__import__("time").time() * 1000)
        f.write_text(
            json.dumps(
                {
                    "usageScope": "turn",
                    "model": "kimi-k2",
                    "time": ts_ms,
                    "usage.record": True,
                    "usage": {"inputOther": 1000, "output": 500, "inputCacheRead": 0, "inputCacheCreation": 0},
                }
            )
            + "\n"
        )
        recs = spend._kimi(0)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["source"], "kimi")
        self.assertEqual(recs[0]["input"], 1000)
        self.assertEqual(recs[0]["output"], 500)
        self.assertEqual(recs[0]["cost_kind"], "plan")

    def test_kimi_session_scope_rollup_skipped(self):
        f = self.root / "kimi" / "wd_1" / "session_1" / "agents" / "main" / "wire.jsonl"
        f.parent.mkdir(parents=True)
        f.write_text(json.dumps({"usageScope": "session", "usage": {"output": 1}}) + "\n")
        self.assertEqual(spend._kimi(0), [])

    # ---------------------------------------------------------------- claude

    def test_claude_parser_prices_estimate(self):
        f = self.root / "claude" / "some-project" / "transcript.jsonl"
        f.parent.mkdir(parents=True)
        f.write_text(
            json.dumps(
                {
                    "timestamp": "2026-08-01T12:00:00Z",
                    "message": {
                        "id": "m1",
                        "model": "claude-sonnet",
                        "usage": {
                            "input_tokens": 1000,
                            "output_tokens": 500,
                            "cache_read_input_tokens": 0,
                            "cache_creation_input_tokens": 0,
                        },
                    },
                }
            )
            + "\n"
        )
        recs = spend._claude(0)
        self.assertEqual(len(recs), 1)
        r = recs[0]
        self.assertEqual(r["source"], "claude")
        self.assertEqual(r["cost_kind"], "estimate")
        self.assertGreater(r["usd"], 0)  # 1000 * $3/M + 500 * $15/M

    def test_claude_synthetic_warmup_skipped(self):
        f = self.root / "claude" / "p" / "t.jsonl"
        f.parent.mkdir(parents=True)
        f.write_text(
            json.dumps(
                {
                    "timestamp": "2026-08-01T12:00:00Z",
                    "message": {"model": "<synthetic>", "usage": {"input_tokens": 1}},
                }
            )
            + "\n"
        )
        self.assertEqual(spend._claude(0), [])

    # ---------------------------------------------------------------- opencode

    def test_opencode_parser_actual_cost(self):
        db = sqlite3.connect(self.root / "opencode.db")
        db.execute("CREATE TABLE message (time_created INTEGER, data TEXT)")
        now_ms = int(__import__("time").time() * 1000)
        db.execute(
            "INSERT INTO message VALUES (?, ?)",
            (
                now_ms,
                json.dumps(
                    {
                        "role": "assistant",
                        "modelID": "opencode/some-model",
                        "providerID": "openrouter",
                        "tokens": {"input": 800, "output": 400, "reasoning": 0, "cache": {"read": 0, "write": 0}},
                        "cost": 0.0123,
                    }
                ),
            ),
        )
        db.commit()
        db.close()
        recs = spend._opencode(0)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["source"], "opencode")
        self.assertEqual(recs[0]["cost_kind"], "actual")
        self.assertEqual(recs[0]["usd"], 0.0123)

    def test_opencode_local_model_free(self):
        db = sqlite3.connect(self.root / "opencode.db")
        db.execute("CREATE TABLE message (time_created INTEGER, data TEXT)")
        now_ms = int(__import__("time").time() * 1000)
        db.execute(
            "INSERT INTO message VALUES (?, ?)",
            (
                now_ms,
                json.dumps(
                    {
                        "role": "assistant",
                        "modelID": "ollama/qwen",
                        "providerID": "ollama",
                        "tokens": {"input": 10, "output": 10, "reasoning": 0, "cache": {"read": 0, "write": 0}},
                    }
                ),
            ),
        )
        db.commit()
        db.close()
        recs = spend._opencode(0)
        self.assertEqual(recs[0]["cost_kind"], "free")
        self.assertEqual(recs[0]["usd"], 0.0)

    # ---------------------------------------------------------------- rollups + defense

    def test_daily_and_summary_rollup(self):
        kimi = self.root / "kimi" / "wd_1" / "session_1" / "agents" / "main" / "wire.jsonl"
        kimi.parent.mkdir(parents=True)
        ts_ms = int(__import__("time").time() * 1000)
        kimi.write_text(
            json.dumps(
                {
                    "usageScope": "turn",
                    "model": "kimi-k2",
                    "time": ts_ms,
                    "usage.record": True,
                    "usage": {"inputOther": 1000, "output": 500, "inputCacheRead": 0, "inputCacheCreation": 0},
                }
            )
            + "\n"
        )
        spend._MEMO.clear()
        s = spend.summary(1)
        self.assertEqual(s["sources_present"]["kimi"], True)
        self.assertGreaterEqual(s["records"], 1)

    def test_missing_dirs_never_raise(self):
        self._patch_paths(kimi=False, claude=False, opencode=False)
        spend._MEMO.clear()
        self.assertEqual(spend.records(1), [])
        self.assertEqual(spend.daily(1), [])
        self.assertEqual(spend.summary(1)["records"], 0)


if __name__ == "__main__":
    unittest.main()
