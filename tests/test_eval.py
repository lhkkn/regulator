"""Unit tests for conductor/eval/harness.py — verdict parsing, judging, summary."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conductor.eval import harness  # noqa: E402


# The shipped golden set exercises tools that need the full stack (context.py, the
# workspace, the machine). Locally that's present; in CI it isn't, so this one case
# skips and CI still guards lint/types + the pure-logic suite.
def _full_stack_present() -> bool:
    try:
        from conductor.projects import find_workspace_root

        find_workspace_root()
    except Exception:
        return False
    return (Path(__file__).resolve().parents[1].parent / "harness").is_dir()


def _resp(text, is_error=False):
    return {"result": {"content": [{"type": "text", "text": text}], "isError": is_error}}


class YesNoTest(unittest.TestCase):
    def test_explicit_verdict_lines(self):
        self.assertTrue(harness._parse_yes_no("blah\nVERDICT: YES"))
        self.assertFalse(harness._parse_yes_no("blah\nVERDICT: NO"))

    def test_gist_fallback(self):
        self.assertTrue(harness._parse_yes_no("YES overall"))
        self.assertFalse(harness._parse_yes_no("NO"))
        self.assertFalse(harness._parse_yes_no(""))


class ResponseTextTest(unittest.TestCase):
    def test_tool_content(self):
        text, is_err, code = harness._response_text(_resp("hello"))
        self.assertEqual(text, "hello")
        self.assertFalse(is_err)
        self.assertIsNone(code)

    def test_rpc_error(self):
        text, is_err, code = harness._response_text({"error": {"code": -32602, "message": "bad"}})
        self.assertIsNone(text)
        self.assertTrue(is_err)
        self.assertEqual(code, -32602)


class JudgeTest(unittest.TestCase):
    def _case(self, expect):
        return harness.Case("t", {}, expect)

    def test_contains_pass(self):
        status, _ = harness._judge(self._case({"contains": ["drift"], "not_contains": ["boom"]}), _resp("drift found"))
        self.assertEqual(status, "pass")

    def test_missing_substring_fails(self):
        status, detail = harness._judge(self._case({"contains": ["drift"]}), _resp("all clear"))
        self.assertEqual(status, "fail")
        self.assertIn("drift", detail)

    def test_is_error_expectation(self):
        self.assertEqual(harness._judge(self._case({"is_error": True}), _resp("denied", is_error=True))[0], "pass")
        self.assertEqual(harness._judge(self._case({"is_error": False}), _resp("denied", is_error=True))[0], "fail")


class SummaryTest(unittest.TestCase):
    def test_ok_requires_no_failures(self):
        self.assertTrue(harness.summarize([{"status": "pass"}, {"status": "skip"}])["ok"])
        self.assertFalse(harness.summarize([{"status": "pass"}, {"status": "fail"}])["ok"])
        self.assertFalse(harness.summarize([])["ok"])


class GoldenTest(unittest.TestCase):
    def test_loader_skips_comments_and_blanks(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "g.jsonl"
            p.write_text("# comment\n" + json.dumps({"name": "a", "request": {}, "expect": {}}) + "\n\n")
            cases = harness.load_golden(p)
        self.assertEqual([c.name for c in cases], ["a"])

    @unittest.skipUnless(_full_stack_present(), "full stack not present (CI runs lint + pure tests)")
    def test_shipped_golden_set_is_green(self):
        ok, report = harness.run()
        self.assertTrue(ok, report)


if __name__ == "__main__":
    unittest.main()
