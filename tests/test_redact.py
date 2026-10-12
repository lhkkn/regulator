"""Unit tests for conductor/redact.py — the shared secret-masking primitive.

Tiny module, security-relevant: cover the key cases explicitly rather than only
exercising it through the trace/ledger tests.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conductor.redact import redact  # noqa: E402


class RedactTest(unittest.TestCase):
    def test_masks_secret_keys_recursively(self):
        out = redact({"token": "x", "nested": {"password": "y"}, "list": [{"secret": "z"}], "ok": 1})
        self.assertEqual(out["token"], "***")
        self.assertEqual(out["nested"]["password"], "***")
        self.assertEqual(out["list"][0]["secret"], "***")
        self.assertEqual(out["ok"], 1)

    def test_non_secret_keys_are_kept(self):
        out = redact({"key_id": "k", "project": "foo"})
        self.assertEqual(out["key_id"], "k")  # 'key_id' is not a secret hint
        self.assertEqual(out["project"], "foo")

    def test_max_str_caps_long_strings(self):
        self.assertEqual(redact({"blob": "x" * 100}, max_str=10)["blob"], "x" * 10 + "…")

    def test_without_max_str_strings_are_untouched(self):
        self.assertEqual(redact({"blob": "x" * 100})["blob"], "x" * 100)

    def test_tuples_are_returned_as_lists(self):
        self.assertEqual(redact(("a", "b")), ["a", "b"])

    def test_passthrough_for_scalars(self):
        self.assertEqual(redact(42), 42)
        self.assertIsNone(redact(None))


if __name__ == "__main__":
    unittest.main()
