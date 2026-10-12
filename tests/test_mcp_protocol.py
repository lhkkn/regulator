"""Protocol negotiation for the MCP server — the conformance-honesty contract.

`initialize` used to echo whatever version the client asked for while implementing
2024-11-05 semantics. A client negotiating `2026-07-28` (stateless requests,
per-request capability negotiation, Elicitation-only) was told "agreed" and then got
2024 behaviour — a false claim of conformance that fails silently.

These tests pin the fix: the server answers with a version it implements, always, and
says so on stderr when it has to downgrade.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conductor.mcp import server  # noqa: E402


class NegotiateProtocolTest(unittest.TestCase):
    def test_supported_version_is_echoed(self):
        """If we implement exactly what was asked for, agree to it."""
        for v in server.SUPPORTED_PROTOCOL_VERSIONS:
            with self.subTest(v=v):
                self.assertEqual(server.negotiate_protocol(v), v)

    def test_missing_version_falls_back_to_our_newest(self):
        self.assertEqual(server.negotiate_protocol(None), server.PROTOCOL_VERSION)
        self.assertEqual(server.negotiate_protocol(""), server.PROTOCOL_VERSION)

    def test_unknown_version_never_claims_conformance(self):
        """The bug being fixed: an unimplemented revision must not be echoed back."""
        with mock.patch.object(server, "_warn"):
            for v in ("2025-11-25", "2026-07-28", "1999-01-01"):
                with self.subTest(v=v):
                    got = server.negotiate_protocol(v)
                    self.assertNotEqual(got, v)
                    self.assertIn(got, server.SUPPORTED_PROTOCOL_VERSIONS)

    def test_downgrade_is_reported_on_stderr(self):
        with mock.patch.object(server, "_warn") as warn:
            server.negotiate_protocol("2026-07-28")
        warn.assert_called_once()
        self.assertIn("2026-07-28", warn.call_args.args[0])


class InitializeTest(unittest.TestCase):
    def test_initialize_reports_only_supported_versions(self):
        with mock.patch.object(server, "_warn"):
            result = server.dispatch("initialize", {"protocolVersion": "2026-07-28"})
        self.assertIn(result["protocolVersion"], server.SUPPORTED_PROTOCOL_VERSIONS)

    def test_initialize_declares_tools_capability_only(self):
        """We implement tools and nothing else — the capability set must say so."""
        result = server.dispatch("initialize", {"protocolVersion": "2024-11-05"})
        self.assertEqual(set(result["capabilities"]), {"tools"})
        self.assertEqual(result["serverInfo"]["name"], "regulator")

    def test_protocol_version_is_the_newest_supported(self):
        self.assertEqual(server.PROTOCOL_VERSION, server.SUPPORTED_PROTOCOL_VERSIONS[0])
