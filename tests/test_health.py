"""Unit tests for conductor/health.py — probe parsing + offline degradation."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conductor import health  # noqa: E402


class ParseTest(unittest.TestCase):
    OUTPUT = (
        "AI stack check\n"
        "legend: id=status\n"
        "  deepseek_main      OK  ($12.00)\n"
        "  openai_main        STALE (no credit)\n"
        "  schwab_oauth       12m left\n"
        "  github_oauth       -3m left\n"
    )

    def test_parse_statuses(self):
        entries, _ = health._parse(self.OUTPUT)
        by = {e["id"]: e for e in entries}
        self.assertTrue(by["deepseek_main"]["ok"])
        self.assertFalse(by["openai_main"]["ok"])  # STALE
        self.assertTrue(by["schwab_oauth"]["ok"])  # positive minutes left
        self.assertFalse(by["github_oauth"]["ok"])  # past expiry

    def test_flags_only_bad_entries(self):
        _, flags = health._parse(self.OUTPUT)
        self.assertTrue(any("openai_main" in f for f in flags))
        self.assertTrue(any("github_oauth" in f for f in flags))
        self.assertFalse(any("deepseek_main" in f for f in flags))

    def test_headers_are_skipped(self):
        entries, flags = health._parse("legend\n")
        self.assertEqual(entries, [])
        self.assertEqual(flags, [])


class OfflineTest(unittest.TestCase):
    def test_probe_offline_is_inert(self):
        d = health.probe(offline=True)
        self.assertFalse(d["ok"])
        self.assertEqual(d["entries"], [])
        self.assertEqual(d["flags"], [])

    def test_section_offline(self):
        flags, body = health.section(offline=True)
        self.assertEqual(flags, [])
        self.assertIn("offline", body)

    def test_report_offline_says_unavailable(self):
        self.assertIn("unavailable", health.report(offline=True))


if __name__ == "__main__":
    unittest.main()
