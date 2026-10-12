"""Unit tests for conductor/trace.py — the MCP call log (tmp app dir) + redaction."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conductor import trace  # noqa: E402
from tests.support import EnvTestCase  # noqa: E402


class TraceTest(EnvTestCase):
    def setUp(self):
        super().setUp()
        self.set_env(CONDUCTOR_APP_DIR=self.tmp.name)

    def test_record_then_load(self):
        trace.record("brief", {"x": 1}, ok=True, ms=1.2, out_bytes=5)
        entry = trace.load(1)[0]
        self.assertEqual(entry["tool"], "brief")
        self.assertTrue(entry["ok"])
        self.assertIn("brief", trace.render([entry]))

    def test_redacts_secretish_args(self):
        trace.record("t", {"token": "abc", "password": "x", "key_id": "k"}, ok=True, ms=0)
        args = trace.load(1)[0]["args"]
        self.assertEqual(args["token"], "***")
        self.assertEqual(args["password"], "***")
        self.assertEqual(args["key_id"], "k")  # key_id is not a secret

    def test_error_is_recorded(self):
        trace.record("t", {}, ok=False, ms=0, error="boom")
        self.assertEqual(trace.load(1)[0]["error"], "boom")

    def test_render_empty(self):
        self.assertEqual(trace.render([]), "traces: none recorded")


if __name__ == "__main__":
    unittest.main()
