"""Tests that render/reconcile resolve paths at call time (no import-time capture)
and return data rather than printing — the refactor's whole point."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conductor.projects import reconcile, render  # noqa: E402
from tests.support import EnvTestCase  # noqa: E402


class RedirectTest(EnvTestCase):
    def setUp(self):
        super().setUp()
        ws = self.tmpdir / "ws"
        (ws / ".workspace-root").mkdir(parents=True)
        (ws / "registry").mkdir()
        self.reg = ws / "registry" / "registry.toml"
        self.reg.write_text('[[project]]\nname = "demo"\nstatus = "active"\ndeploy = "none"\ntier = "personal"\n')
        self.ws = ws.resolve()  # find_workspace_root() resolves symlinks (/var -> /private/var)
        self.set_env(CONDUCTOR_WORKSPACE_ROOT=str(ws), CONDUCTOR_REGISTRY_FILE=str(self.reg))

    def test_render_paths_are_call_time_not_import_time(self):
        self.assertEqual(render._reg(), self.reg)
        self.assertEqual(render._root(), self.ws)
        code, text = render.run(check=True)  # nothing generated yet
        self.assertEqual(code, 1)
        self.assertIn("STALE", text)
        code, text = render.run()  # write into the redirected workspace
        self.assertEqual(code, 0)
        self.assertTrue((self.ws / "registry" / "REGISTRY.md").exists())
        self.assertTrue((self.ws / "README.md").exists())
        code, text = render.run(check=True)
        self.assertEqual(code, 0)
        self.assertIn("up to date", text)

    def test_reconcile_returns_data_not_prints(self):
        code, text = reconcile.run(local=True)
        self.assertIsInstance(code, int)
        self.assertIn("Local working trees", text)
        self.assertIn("All clear", text)


if __name__ == "__main__":
    unittest.main()
