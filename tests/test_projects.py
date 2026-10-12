"""Unit tests for conductor/projects/__init__.py — registry path + grouping.

registry_path() honors CONDUCTOR_REGISTRY_FILE, so the tests point it at a temp
registry.toml and assert grouping without touching the real workspace registry.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import conductor.projects as projects  # noqa: E402
from tests.support import EnvTestCase, write_registry  # noqa: E402


class RegistryGroupingTest(EnvTestCase):
    def setUp(self):
        super().setUp()
        self.reg = self.tmpdir / "registry.toml"
        write_registry(
            self.reg,
            [
                {"name": "media", "status": "active"},
                {"name": "conductor", "status": "active"},
                {"name": "davinci-resolve", "status": "archived"},
            ],
        )
        self.set_env(CONDUCTOR_REGISTRY_FILE=str(self.reg))

    def test_registry_path_uses_env(self):
        self.assertEqual(projects.registry_path(), self.reg)

    def test_group_by_status(self):
        groups = projects.group_by_status()
        self.assertEqual(sorted(p["name"] for p in groups["active"]), ["conductor", "media"])
        self.assertEqual([p["name"] for p in groups["archived"]], ["davinci-resolve"])


if __name__ == "__main__":
    unittest.main()
