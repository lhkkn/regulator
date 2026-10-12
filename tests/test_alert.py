"""Unit tests for conductor/alert.py — the ops alerting loop.

Focus on the de-duplication decision (pure logic) so an unchanged problem doesn't
nag on every run. Network and Keychain are never touched.
"""

import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conductor import alert  # noqa: E402


def _state(flags, hours_ago=0.0):
    return {"flags": flags, "sent_at": (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat()}


class ShouldSendTest(unittest.TestCase):
    def test_force_always_sends(self):
        self.assertTrue(alert._should_send([], _state([]), force=True))

    def test_changed_flags_send(self):
        self.assertTrue(alert._should_send(["a"], _state(["b"]), force=False))

    def test_unchanged_within_window_does_not_send(self):
        self.assertFalse(alert._should_send(["a"], _state(["a"], hours_ago=1), force=False))

    def test_unchanged_after_window_resends(self):
        self.assertTrue(alert._should_send(["a"], _state(["a"], hours_ago=25), force=False))

    def test_unchanged_all_clear_never_sends(self):
        self.assertFalse(alert._should_send([], _state([], hours_ago=100), force=False))

    def test_recovery_sends_all_clear(self):
        # previous run had flags, now none → notify the all-clear once
        self.assertTrue(alert._should_send([], _state(["a"]), force=False))

    def test_missing_sent_at_sends(self):
        self.assertTrue(alert._should_send(["a"], {"flags": ["a"]}, force=False))


class PostTest(unittest.TestCase):
    def test_post_headers_and_url(self):
        captured = {}

        class _Resp:
            def read(self):
                return b""

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=0):
            captured["url"] = req.full_url
            captured["headers"] = {k.lower(): v for k, v in req.header_items()}
            captured["data"] = req.data
            return _Resp()

        real = alert.urllib.request.urlopen
        alert.urllib.request.urlopen = fake_urlopen
        try:
            alert._post("topic1", "https://ntfy.sh", ["low provider balance: $3.00"])
        finally:
            alert.urllib.request.urlopen = real

        self.assertEqual(captured["url"], "https://ntfy.sh/topic1")
        self.assertEqual(captured["headers"]["priority"], "high")  # balance ⇒ urgent
        self.assertIn(b"balance", captured["data"])


if __name__ == "__main__":
    unittest.main()
