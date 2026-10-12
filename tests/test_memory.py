"""Unit tests for conductor/memory.py — the durable cross-session store (tmp db)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conductor import memory  # noqa: E402
from tests.support import EnvTestCase  # noqa: E402


class MemoryTest(EnvTestCase):
    def setUp(self):
        super().setUp()
        self.set_env(CONDUCTOR_MEMORY_DB=str(self.tmpdir / "mem.db"))

    def test_add_search_list(self):
        memory.add("the stack lives at ~/Stack", kind="fact", tags="arch")
        memory.add("domains are split by purpose", kind="decision")
        self.assertEqual(len(memory.search("stack")), 1)
        self.assertEqual(len(memory.search("arch")), 1)
        self.assertGreaterEqual(len(memory.recent()), 2)

    def test_search_is_empty_when_nothing_matches(self):
        memory.add("hello")
        self.assertEqual(memory.search("zzz-nope"), [])

    def test_render_empty(self):
        self.assertEqual(memory.render([]), "(none)")

    def test_render_has_kind_and_tag(self):
        memory.add("remember me", kind="fact", tags="t")
        out = memory.render(memory.recent())
        self.assertIn("remember me", out)
        self.assertIn("#t", out)


if __name__ == "__main__":
    unittest.main()
