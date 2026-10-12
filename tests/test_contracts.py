"""Unit tests for conductor/contracts.py — the concrete actuator contracts."""

import json
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conductor import contracts  # noqa: E402
from conductor.mcp.tools import TOOLS  # noqa: E402
from tests.support import EnvTestCase  # noqa: E402


class RegistryTest(unittest.TestCase):
    def test_every_write_tool_has_a_contract(self):
        write_tools = {t["name"] for t in TOOLS if t.get("risk") == "write"}
        self.assertEqual({c.tool for c in contracts.list_contracts()}, write_tools)

    def test_lookup_by_name_and_tool(self):
        by_name = contracts.get("backup.run")
        self.assertIsNotNone(by_name)
        self.assertIs(by_name, contracts.get("backup_run"))
        self.assertIsNone(contracts.get("nope"))

    def test_render_list(self):
        out = contracts.render_list()
        self.assertIn("backup.run", out)
        self.assertIn("projects.register", out)

    def test_run_tool_unknown(self):
        text, is_error = contracts.run_tool("conductor.nope", {})
        self.assertTrue(is_error)
        self.assertIn("unknown contract", text)


class BackupContractTest(unittest.TestCase):
    def test_precondition_refuses_when_fresh(self):
        with mock.patch("conductor.backup.latest", return_value={"ok": True, "age_hours": 1}):
            ok, detail = contracts._backup_precondition({})
        self.assertFalse(ok)
        self.assertIn("already fresh", detail)

    def test_precondition_allows_when_stale(self):
        with mock.patch("conductor.backup.latest", return_value={"ok": True, "age_hours": 99}):
            ok, _ = contracts._backup_precondition({})
        self.assertTrue(ok)

    def test_force_bypasses_freshness(self):
        with mock.patch("conductor.backup.latest", return_value={"ok": True, "age_hours": 1}):
            ok, _ = contracts._backup_precondition({"force": True})
        self.assertTrue(ok)

    def test_verify_checks_returncode_then_freshness(self):
        bad = contracts._backup_verify({}, {"returncode": 2, "stderr_tail": "nope"})
        self.assertFalse(bad[0])
        with mock.patch("conductor.backup.latest", return_value={"ok": True, "age_hours": 1}):
            good = contracts._backup_verify({}, {"returncode": 0})
        self.assertTrue(good[0])


class RegisterContractTest(EnvTestCase):
    def setUp(self):
        super().setUp()
        self.set_env(CONDUCTOR_APP_DIR=self.tmp.name)

    def test_precondition_validates(self):
        self.assertFalse(contracts._register_precondition({})[0])
        self.assertFalse(contracts._register_precondition({"project": "a/b"})[0])
        with mock.patch.object(contracts, "_locate", return_value=None):
            self.assertFalse(contracts._register_precondition({"project": "foo"})[0])
        with (
            mock.patch.object(contracts, "_locate", return_value=Path("/x/foo")),
            mock.patch.object(contracts, "_registered", return_value=True),
        ):
            self.assertFalse(contracts._register_precondition({"project": "foo"})[0])
        with (
            mock.patch.object(contracts, "_locate", return_value=Path("/x/foo")),
            mock.patch.object(contracts, "_registered", return_value=False),
        ):
            self.assertTrue(contracts._register_precondition({"project": "foo"})[0])

    def test_act_writes_and_rollback_restores(self):
        reg = Path(self.tmp.name) / "registry.toml"
        reg.write_text('# reg\n\n[meta]\nproduction_host = "prodhost"\n', encoding="utf-8")
        prior = reg.read_text(encoding="utf-8")
        with (
            mock.patch.object(contracts, "registry_path", return_value=reg),
            mock.patch.object(contracts, "_render", return_value=(0, "")),
        ):
            effect = contracts._register_act({"project": "foo", "tier": "personal"})
            self.assertEqual(effect["_prior"], prior)
            data = tomllib.loads(reg.read_text(encoding="utf-8"))
            self.assertEqual(data["project"][0]["name"], "foo")
            ok, _ = contracts._register_verify({"project": "foo"}, effect)
            self.assertTrue(ok)
            contracts._register_rollback({"project": "foo"}, effect)
            self.assertEqual(reg.read_text(encoding="utf-8"), prior)

    def test_run_tool_dry_run_changes_nothing(self):
        reg = Path(self.tmp.name) / "registry.toml"
        reg.write_text("[meta]\n", encoding="utf-8")
        before = reg.read_text(encoding="utf-8")
        with (
            mock.patch.object(contracts, "registry_path", return_value=reg),
            mock.patch.object(contracts, "_render", return_value=(0, "")),
            mock.patch.object(contracts, "_locate", return_value=Path("/x/foo")),
        ):
            text, is_error = contracts.run_tool("projects_register", {"project": "foo"})
        self.assertFalse(is_error)
        self.assertIn("dry-run", text)
        self.assertEqual(reg.read_text(encoding="utf-8"), before)

    def test_run_tool_apply_writes_and_verifies(self):
        reg = Path(self.tmp.name) / "registry.toml"
        reg.write_text("[meta]\n", encoding="utf-8")
        with (
            mock.patch.object(contracts, "registry_path", return_value=reg),
            mock.patch.object(contracts, "_render", return_value=(0, "")),
            mock.patch.object(contracts, "_locate", return_value=Path("/x/foo")),
        ):
            text, is_error = contracts.run_tool("projects_register", {"project": "foo", "apply": True})
        self.assertFalse(is_error)
        self.assertIn("verified", text)
        self.assertIn("foo", reg.read_text(encoding="utf-8"))


class DocsContractTest(unittest.TestCase):
    def test_precondition_reflects_staleness(self):
        from conductor.projects import render

        with mock.patch.object(render, "run", return_value=(1, "STALE")):
            self.assertTrue(contracts._docs_precondition({})[0])
        with mock.patch.object(render, "run", return_value=(0, "up to date")):
            self.assertFalse(contracts._docs_precondition({})[0])

    def test_verify_checks_render(self):
        from conductor.projects import render

        with mock.patch.object(render, "run", return_value=(0, "up to date")):
            self.assertTrue(contracts._docs_verify({}, {})[0])
        with mock.patch.object(render, "run", return_value=(1, "STALE")):
            self.assertFalse(contracts._docs_verify({}, {})[0])

    def test_act_captures_prior_and_rollback_restores(self):
        from conductor.projects import render

        with tempfile.TemporaryDirectory() as tmp:
            a = Path(tmp) / "REGISTRY.md"
            b = Path(tmp) / "README.md"
            a.write_text("old-a")
            b.write_text("old-b")
            with (
                mock.patch.object(render, "targets", return_value=(a, b)),
                mock.patch.object(render, "run", return_value=(0, "wrote")),
            ):
                effect = contracts._docs_act({})
            self.assertEqual(effect["_prior"][str(a)], "old-a")
            a.write_text("new-a")
            b.unlink()
            contracts._docs_rollback({}, effect)
            self.assertEqual(a.read_text(), "old-a")
            self.assertEqual(b.read_text(), "old-b")


class KeysRetireContractTest(EnvTestCase):
    def setUp(self):
        super().setUp()
        self.reg = self.tmpdir / "keys.json"
        self.set_env(CONDUCTOR_KEYS_REGISTRY=str(self.reg))

    def test_precondition_act_verify_rollback(self):
        self.reg.write_text(
            json.dumps(
                {
                    "keys": [
                        {"id": "dead", "locations": [], "notes": "REVOKED 2026"},
                        {"id": "live", "locations": [{"path": "x"}], "notes": "REVOKED but in use"},
                        {"id": "orphan", "locations": [], "notes": "orphaned"},
                    ]
                }
            )
        )
        ok, detail = contracts._retire_precondition({})
        self.assertTrue(ok)
        self.assertIn("dead", detail)
        self.assertIn("orphan", detail)
        effect = contracts._retire_act({})
        self.assertEqual(effect["retired"], ["dead", "orphan"])
        remaining = [k["id"] for k in json.loads(self.reg.read_text())["keys"]]
        self.assertEqual(remaining, ["live"])
        self.assertTrue(contracts._retire_verify({}, effect)[0])
        contracts._retire_rollback({}, effect)
        self.assertEqual(len(json.loads(self.reg.read_text())["keys"]), 3)

    def test_noop_when_no_dead_keys(self):
        self.reg.write_text(
            json.dumps(
                {
                    "keys": [
                        {"id": "live", "locations": [{"path": "x"}], "notes": ""},
                    ]
                }
            )
        )
        self.assertFalse(contracts._retire_precondition({})[0])


class RegisterDriftContractTest(EnvTestCase):
    def setUp(self):
        super().setUp()
        root = self.tmpdir / "ws"
        (root / ".workspace-root").mkdir(parents=True)
        (root / "registry").mkdir()
        (root / "work" / "apps" / "known").mkdir(parents=True)
        (root / "work" / "apps" / "newone").mkdir(parents=True)
        (root / "work" / "research" / "lens").mkdir(parents=True)
        self.root = root
        self.reg = root / "registry" / "registry.toml"
        self.reg.write_text('[[project]]\nname = "known"\nstatus = "active"\ndeploy = "none"\n')
        self.set_env(CONDUCTOR_WORKSPACE_ROOT=str(root), CONDUCTOR_REGISTRY_FILE=str(self.reg))

    def test_unregistered_dirs(self):
        self.assertEqual(contracts._unregistered_dirs(), ["newone", "lens"])

    def test_precondition_lists_drift(self):
        ok, detail = contracts._register_drift_precondition({})
        self.assertTrue(ok)
        self.assertIn("newone", detail)
        self.assertIn("lens", detail)

    def test_act_verify_rollback(self):
        prior = self.reg.read_text(encoding="utf-8")
        with mock.patch.object(contracts, "_render", return_value=(0, "")):
            effect = contracts._register_drift_act({})
            self.assertEqual(effect["registered"], ["newone", "lens"])
            data = tomllib.loads(self.reg.read_text(encoding="utf-8"))
            self.assertEqual(sorted(p["name"] for p in data["project"]), ["known", "lens", "newone"])
            ok, detail = contracts._register_drift_verify({}, effect)
            self.assertTrue(ok, detail)
            contracts._register_rollback({}, effect)
        self.assertEqual(self.reg.read_text(encoding="utf-8"), prior)

    def test_precondition_refuses_when_no_drift(self):
        with mock.patch.object(contracts, "_render", return_value=(0, "")):
            contracts._register_drift_act({})
        self.assertFalse(contracts._register_drift_precondition({})[0])


class ArchiveContractTest(EnvTestCase):
    """The archive actuator must have a repair path.

    A half-done archive (status flipped, directory still live) is a real state — it is
    what you get from a hand-edit, or from a run that died between the move and the
    write. Refusing it left no way back through the actuator, which is the one place
    the rollback and the ledger live.
    """

    def _check(self, status, live_dir, target_exists=False):
        """Run the precondition with the filesystem edges stubbed out."""
        with (
            mock.patch.object(contracts, "_project_status", return_value=status),
            mock.patch.object(contracts, "_locate", return_value=(live_dir or None)),
            mock.patch.object(contracts, "find_workspace_root", return_value=self.tmpdir),
            mock.patch.object(contracts.Path, "exists", lambda self_: target_exists),
        ):
            return contracts._archive_precondition({"project": "demo"})

    def test_active_project_is_archivable(self):
        ok, why = self._check("active", Path("/ws/work/apps/demo"))
        self.assertTrue(ok)
        self.assertIn("-> work/archive/demo", why)

    def test_half_done_archive_is_repairable(self):
        """status says archived, but the directory never left the live tree."""
        ok, why = self._check("archived", Path("/ws/work/apps/demo"))
        self.assertTrue(ok)
        self.assertIn("completing the move", why)

    def test_fully_archived_project_is_refused(self):
        ok, why = self._check("archived", None)
        self.assertFalse(ok)
        self.assertIn("already archived", why)

    def test_existing_target_is_refused(self):
        ok, why = self._check("active", Path("/ws/work/apps/demo"), target_exists=True)
        self.assertFalse(ok)
        self.assertIn("already exists", why)

    def test_invalid_name_is_refused(self):
        for bad in ("", "a/b", "a b", ".."):
            with self.subTest(bad=bad):
                self.assertFalse(contracts._archive_precondition({"project": bad})[0])

    def test_unregistered_project_is_refused(self):
        with mock.patch.object(contracts, "_project_status", return_value=None):
            self.assertFalse(contracts._archive_precondition({"project": "ghost"})[0])
