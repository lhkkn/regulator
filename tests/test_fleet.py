"""Unit tests for conductor/fleet — the declared run-plane registry.

`fleet_path()` honors CONDUCTOR_FLEET_FILE and `declared()` reads the registry via
CONDUCTOR_REGISTRY_FILE, so the tests build both in a temp dir and assert the merge,
the plane classification, and the offline render — never touching the real fleet
or probing the network.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conductor import fleet  # noqa: E402
from conductor.projects.checks import is_vendor_unit  # noqa: E402
from tests.support import EnvTestCase, write_registry  # noqa: E402


def write_fleet(path: Path, services: list[dict]) -> None:
    lines: list[str] = []
    for service in services:
        lines.append("[[service]]")
        for key, value in service.items():
            lines.append(f'{key} = "{value}"')
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


class FleetDeclaredTest(EnvTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.reg = self.tmpdir / "registry.toml"
        write_registry(
            self.reg,
            [
                {
                    "name": "finance",
                    "status": "active",
                    "service": "finance.service",
                    "port": "8051",
                    "url": "https://finance.example.com",
                    "title": "Finance",
                },
                {"name": "curie", "status": "archived", "service": "curie.service"},
                {"name": "composite", "status": "active"},
            ],
        )
        self.spec = self.tmpdir / "fleet.toml"
        write_fleet(
            self.spec,
            [
                {
                    "name": "console",
                    "plane": "operator",
                    "runtime": "launchagent",
                    "host": "mac",
                    "unit": "com.example.console",
                },
                {
                    "name": "monitor",
                    "plane": "operator",
                    "runtime": "systemd",
                    "host": "prodhost",
                    "unit": "glances.service",
                    "url": "https://prodhost.example.com",
                },
            ],
        )
        self.set_env(CONDUCTOR_REGISTRY_FILE=str(self.reg), CONDUCTOR_FLEET_FILE=str(self.spec))

    def test_declared_merges_fleet_and_registry(self):
        names = {s["name"] for s in fleet.declared()}
        self.assertIn("console", names)
        self.assertIn("monitor", names)
        self.assertIn("finance", names)
        self.assertNotIn("curie", names)  # archived projects are skipped
        self.assertNotIn("composite", names)  # no `service` field -> not a service

    def test_registry_service_is_a_domain_plane(self):
        finance = next(s for s in fleet.declared() if s["name"] == "finance")
        self.assertEqual(finance["plane"], "domain")
        self.assertEqual(finance["unit"], "finance.service")
        self.assertEqual(finance["bind"], "127.0.0.1:8051")

    def test_operator_sorts_before_domain(self):
        planes = [s["plane"] for s in fleet.declared()]
        self.assertEqual(planes, sorted(planes, key=lambda p: fleet._PLANE_RANK[p]))

    def test_defaults_fill_optional_fields(self):
        console = next(s for s in fleet.declared() if s["name"] == "console")
        self.assertEqual(console["auth"], "none")
        self.assertEqual(console["cost"], "none")

    def test_offline_join_is_declared_only(self):
        rows = fleet.services(probe=False)
        self.assertTrue(rows and all(r["declared"] for r in rows))
        self.assertTrue(all(r["state"] == "unknown" for r in rows))

    def test_render_offline_lists_services(self):
        text = fleet.render(probe=False)
        self.assertIn("fleet —", text)
        self.assertIn("console", text)
        self.assertIn("finance", text)

    def test_summary_counts_planes(self):
        s = fleet.summary(fleet.services(probe=False))
        self.assertEqual(s["planes"]["operator"], 2)
        self.assertEqual(s["planes"]["domain"], 1)
        self.assertEqual(s["declared"], 3)

    def test_missing_fleet_file_degrades_to_registry(self):
        self.set_env(CONDUCTOR_FLEET_FILE=str(self.tmpdir / "absent.toml"))
        self.assertEqual([s["name"] for s in fleet.declared()], ["finance"])


class VendorUnitTest(unittest.TestCase):
    def test_vendor_markers(self):
        for unit in ("systemd-resolved.service", "snap.mesa-2404.component-monitor.service", "docker.service"):
            self.assertTrue(is_vendor_unit(unit), unit)

    def test_ours_are_not_vendor(self):
        for unit in ("glances.service", "cloudflared-monitor.service", "composite-demo.service"):
            self.assertFalse(is_vendor_unit(unit), unit)


if __name__ == "__main__":
    unittest.main()
