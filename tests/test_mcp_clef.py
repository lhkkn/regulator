"""Unit tests for the clef_ask MCP tool handler (no network)."""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conductor import judge  # noqa: E402
from conductor.mcp import server  # noqa: E402


class ClefToolTest(unittest.TestCase):
    def test_returns_answers_json(self):
        answers = {"satisfied": {"type": "noul", "noul": 0.9}}
        with mock.patch.object(judge, "ask", return_value=answers) as ask:
            result = server.call_tool(
                "clef_ask",
                {"state": "x", "questions": {"satisfied": {"type": "noul", "instructions": "?"}}},
            )
        self.assertFalse(result.get("isError"))
        payload = json.loads(result["content"][0]["text"])
        self.assertEqual(payload["answers"]["satisfied"]["noul"], 0.9)
        ask.assert_called_once()
        self.assertEqual(ask.call_args.args[0], "x")

    def test_model_override_is_forwarded(self):
        with mock.patch.object(judge, "ask", return_value={"q": 0.5}) as ask:
            server.call_tool(
                "clef_ask",
                {"state": "x", "questions": {"q": {"type": "noul"}}, "model": "clef-flash"},
            )
        self.assertEqual(ask.call_args.kwargs["model"], "clef-flash")

    def test_empty_questions_is_error(self):
        result = server.call_tool("clef_ask", {"state": "x", "questions": {}})
        self.assertTrue(result.get("isError"))

    def test_clef_error_is_reported(self):
        with mock.patch.object(judge, "ask", side_effect=judge.ClefError("HTTP 401: nope")):
            result = server.call_tool("clef_ask", {"state": "x", "questions": {"q": {"type": "noul"}}})
        self.assertTrue(result.get("isError"))
        self.assertIn("401", result["content"][0]["text"])

    def test_disabled_by_default_reports_clear_error(self):
        with mock.patch.dict(os.environ, {"CONDUCTOR_CLEF_ENABLED": ""}, clear=False):
            judge.reset()
            result = server.call_tool("clef_ask", {"state": "x", "questions": {"q": {"type": "noul"}}})
        self.assertTrue(result.get("isError"))
        self.assertIn("disabled", result["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
