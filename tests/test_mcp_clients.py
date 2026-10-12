"""Unit tests for conductor/mcp_clients.py — who else's tools does your agent load?

The exposure being controlled: a third-party MCP server's tool descriptions enter the
model's context on every request, and an unpinned package runner executes whatever the
index serves today. These tests pin the inventory logic and the two rules.
"""

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conductor import mcp_clients as mc  # noqa: E402


class JsoncStripTest(unittest.TestCase):
    def test_line_comments_are_removed(self):
        self.assertEqual(json.loads(mc._strip_jsonc('{"a":1} // trailing')), {"a": 1})

    def test_block_comments_are_removed(self):
        self.assertEqual(json.loads(mc._strip_jsonc('/* x */ {"a":1}')), {"a": 1})

    def test_slashes_inside_strings_survive(self):
        """The bug a naive strip introduces: it eats `//` inside URLs."""
        src = '{"url":"https://example.com/mcp","cmd":"/usr/local/bin/x"}'
        self.assertEqual(json.loads(mc._strip_jsonc(src)), json.loads(src))

    def test_escaped_quote_does_not_end_the_string(self):
        src = r'{"a":"he said \"// not a comment\""}'
        self.assertEqual(json.loads(mc._strip_jsonc(src)), json.loads(src))


class ServerDiscoveryTest(unittest.TestCase):
    def _config(self, payload, name="opencode.jsonc"):
        d = TemporaryDirectory()
        self.addCleanup(d.cleanup)
        p = Path(d.name) / name
        p.write_text(payload, encoding="utf-8")
        return p

    def test_reads_local_and_remote_servers(self):
        p = self._config(
            json.dumps(
                {
                    "mcp": {
                        "a": {"type": "local", "command": ["/bin/x", "--flag"]},
                        "b": {"type": "remote", "url": "https://example.com/mcp"},
                    }
                }
            )
        )
        got = {s["name"]: s for s in mc._servers_from_json("opencode", p)}
        self.assertEqual(set(got), {"a", "b"})
        self.assertEqual(got["a"]["transport"], "local")
        self.assertEqual(got["b"]["transport"], "remote")

    def test_client_tuning_block_is_not_a_server(self):
        """kimi declares `[mcp.client] tool_call_timeout_ms` — tuning, not a server."""
        p = self._config(
            '[mcp.client]\ntool_call_timeout_ms = 60000\n\n[mcp_servers.real]\ncommand = ["/bin/y"]\n',
            name="kimi.toml",
        )
        got = [s["name"] for s in mc._servers_from_toml("kimi", p)]
        self.assertEqual(got, ["real"])


class PinTest(unittest.TestCase):
    def test_uvx_without_version_is_unpinned(self):
        self.assertIsNone(mc.pin_of({"command": ["/x/uvx", "duckduckgo-mcp-server"]}))

    def test_uvx_with_version_is_pinned(self):
        self.assertEqual(
            mc.pin_of({"command": ["/x/uvx", "duckduckgo-mcp-server==1.2.3"]}),
            "duckduckgo-mcp-server==1.2.3",
        )

    def test_at_latest_is_not_a_pin(self):
        """`@latest` is the absence of a pin wearing a version's clothes."""
        self.assertIsNone(mc.pin_of({"command": ["npx", "some-server@latest"]}))

    def test_npx_scoped_package_with_version_is_pinned(self):
        self.assertTrue(mc.pin_of({"command": ["npx", "@scope/server@1.4.0"]}))

    def test_non_runner_is_unpinned_by_default(self):
        self.assertIsNone(mc.pin_of({"command": ["/usr/local/bin/my-server"]}))


class AuditTest(unittest.TestCase):
    def _audit(self, servers, declarations):
        with (
            mock.patch.object(mc, "servers", return_value=servers),
            mock.patch.object(mc, "declarations", return_value=declarations),
        ):
            return mc.audit()

    def test_undeclared_server_is_a_finding(self):
        r = self._audit(
            [{"harness": "opencode", "name": "mystery", "transport": "local", "command": [], "url": ""}],
            {},
        )
        self.assertEqual(len(r["undeclared"]), 1)
        self.assertIn("not declared", r["findings"][0])

    def test_declared_unpinned_is_a_finding(self):
        """declared with pin = true, but the config runs a bare package name."""
        r = self._audit(
            [{"harness": "opencode", "name": "ddg", "transport": "local", "command": ["uvx", "pkg"], "url": ""}],
            {"opencode/ddg": {"harness": "opencode", "name": "ddg", "pin": True}},
        )
        self.assertEqual(len(r["unpinned"]), 1)
        self.assertTrue(r["findings"])

    def test_declared_pinned_and_satisfied_is_clean(self):
        r = self._audit(
            [{"harness": "opencode", "name": "ddg", "transport": "local", "command": ["uvx", "pkg==1.0.0"], "url": ""}],
            {"opencode/ddg": {"harness": "opencode", "name": "ddg", "pin": True}},
        )
        self.assertEqual(r["findings"], [])

    def test_first_party_needs_no_pin(self):
        r = self._audit(
            [
                {
                    "harness": "opencode",
                    "name": "conductor",
                    "transport": "local",
                    "command": ["python", "-m"],
                    "url": "",
                }
            ],
            {"opencode/conductor": {"harness": "opencode", "name": "conductor", "trust": "first-party", "pin": False}},
        )
        self.assertEqual(r["findings"], [])

    def test_render_lists_every_server(self):
        r = self._audit(
            [{"harness": "opencode", "name": "a", "transport": "local", "command": ["x"], "url": ""}],
            {"opencode/a": {"harness": "opencode", "name": "a", "trust": "first-party"}},
        )
        self.assertIn("opencode/a", mc.render(r))

    def test_missing_declaration_file_declares_nothing(self):
        with mock.patch.object(mc, "DECLARATIONS", Path("/nonexistent/x.toml")):
            self.assertEqual(mc.declarations(), {})
