"""Unit tests for conductor/keys/analysis.py — the masked key-registry analysis.

Pure functions over in-memory registry dicts (no I/O, no secrets). Run via
`make check` (stdlib unittest; the zero-dep house rule — no pytest here).
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conductor.keys import analysis  # noqa: E402


def _registry(keys):
    return {"version": 1, "keys": keys}


def _key(kid, provider="anthropic", suffix="abc", rotate_by=None, locations=None):
    return {
        "id": kid,
        "provider": provider,
        "suffix": suffix,
        "label": kid,
        "rotate_by": rotate_by,
        "locations": locations or [],
    }


class LoadRegistryTest(unittest.TestCase):
    def test_missing_file_degrades_to_empty(self):
        self.assertEqual(analysis.load_registry(Path("/nonexistent/x.json")), {"version": 1, "keys": []})

    def test_malformed_degrades_to_empty(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write("{not json")
            path = fh.name
        try:
            self.assertEqual(analysis.load_registry(Path(path)), {"version": 1, "keys": []})
        finally:
            os.unlink(path)

    def test_valid(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({"version": 1, "keys": [{"id": "k1"}]}, fh)
            path = fh.name
        try:
            data = analysis.load_registry(Path(path))
            self.assertEqual(len(data["keys"]), 1)
        finally:
            os.unlink(path)


class DuplicatesTest(unittest.TestCase):
    def test_single_project_not_duplicate(self):
        reg = _registry(
            [
                _key("k1", locations=[{"project": "media", "host": "mac", "path": ".env"}]),
            ]
        )
        self.assertEqual(analysis.find_duplicates(reg), [])

    def test_shared_across_projects_flagged(self):
        reg = _registry(
            [
                _key(
                    "k1",
                    locations=[
                        {"project": "media", "host": "mac", "path": ".env"},
                        {"project": "finance", "host": "mac", "path": ".env"},
                    ],
                )
            ]
        )
        self.assertEqual(len(analysis.find_duplicates(reg)), 1)

    def test_same_project_across_hosts_not_flagged(self):
        # dev (mac) + prod (prodhost) of one project is the normal pattern.
        reg = _registry(
            [
                _key(
                    "k1",
                    locations=[
                        {"project": "media", "host": "mac", "path": ".env"},
                        {"project": "media", "host": "prodhost", "path": ".env"},
                    ],
                )
            ]
        )
        self.assertEqual(analysis.find_duplicates(reg), [])


class RotationTest(unittest.TestCase):
    def test_past_rotation(self):
        past = (date.today() - timedelta(days=1)).isoformat()
        reg = _registry([_key("k1", rotate_by=past), _key("k2", rotate_by=None)])
        self.assertEqual([k["id"] for k in analysis.find_past_rotation(reg)], ["k1"])
        self.assertEqual([k["id"] for k in analysis.find_missing_rotation(reg)], ["k2"])

    def test_future_rotation_not_overdue(self):
        future = (date.today() + timedelta(days=30)).isoformat()
        reg = _registry([_key("k1", rotate_by=future)])
        self.assertEqual(analysis.find_past_rotation(reg), [])


class FlaggedLocationsTest(unittest.TestCase):
    def test_flagged(self):
        reg = _registry(
            [
                _key("k1", locations=[{"project": "media", "host": "mac", "path": "notes/api.txt", "flagged": True}]),
            ]
        )
        flagged = analysis.find_flagged_locations(reg)
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0]["key_id"], "k1")


class ProviderMatrixTest(unittest.TestCase):
    def test_matrix_statuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            project_dir = base / "app"
            project_dir.mkdir()
            (project_dir / "main.py").write_text("import os\nkey = os.environ.get('ANTHROPIC_API_KEY')\n")
            # key present + used → "present"
            reg = _registry(
                [_key("k_ant", provider="anthropic", locations=[{"project": "app", "host": "mac", "path": ".env"}])]
            )
            rows = analysis.build_provider_matrix(reg, local_project_base=str(base))
            row = next(r for r in rows if r["project"] == "app")
            self.assertEqual(row["status"]["anthropic"], "present")
            self.assertEqual(row["status"]["openai"], "unknown")

    def test_missing_provider_mismatch(self):
        # find_provider_mismatches scans the real workspace projects dir by
        # default, so inject a matrix row and assert the mismatch extraction.
        fake = [
            {
                "host": "mac",
                "project": "app",
                "status": {"anthropic": "present", "openai": "missing", "kimi": "unknown"},
                "present": {"anthropic": "k_ant"},
            }
        ]
        orig = analysis.build_provider_matrix
        analysis.build_provider_matrix = lambda *a, **k: fake
        try:
            mismatches = analysis.find_provider_mismatches({})
        finally:
            analysis.build_provider_matrix = orig
        hits = {m["provider"]: m["status"] for m in mismatches if m["project"] == "app"}
        self.assertEqual(hits, {"openai": "missing"})


class RenderTest(unittest.TestCase):
    def test_render_list_headers(self):
        reg = _registry(
            [
                _key(
                    "k1",
                    provider="kimi",
                    suffix="xyz",
                    rotate_by="2099-01-01",
                    locations=[{"project": "p", "host": "mac", "path": ".env"}],
                )
            ]
        )
        out = analysis.render_list(reg)
        self.assertIn("ID", out)
        self.assertIn("k1", out)
        self.assertIn("...xyz", out)

    def test_render_audit_clean(self):
        reg = _registry([_key("k1", rotate_by="2099-01-01")])
        text, had = analysis.render_audit(reg)
        self.assertIn("Audit passed", text)
        self.assertFalse(had)

    def test_render_audit_findings(self):
        past = (date.today() - timedelta(days=1)).isoformat()
        reg = _registry([_key("k1", rotate_by=past)])
        text, had = analysis.render_audit(reg)
        self.assertTrue(had)
        self.assertIn("past their rotation", text)


class StaleLocationTest(unittest.TestCase):
    """Does the registry still describe reality?

    The incident this exists for: the registry carried
    `finance.service → FINANCE_PASSWORD` for weeks after `bff8e9b` deleted the
    self-login. Nothing compared the claim to the unit, so every reader — human and agent
    — took a deleted variable to be live, and a session went into establishing that
    finance has no password at all.
    """

    def setUp(self):
        self._real = analysis._unit_sets
        self.addCleanup(setattr, analysis, "_unit_sets", self._real)
        self.probed = []

    def _stub(self, answer):
        """Stub the ssh boundary, recording what was asked."""

        def fake(host, unit, var):
            self.probed.append((host, unit, var))
            return answer.get((host, unit, var))

        analysis._unit_sets = fake

    def _unit_key(self, kid, unit="finance.service", var="FINANCE_PASSWORD", host="prodhost"):
        return _key(kid, locations=[{"host": host, "project": "system", "path": unit, "variable": var}])

    def test_a_unit_that_no_longer_sets_the_variable_is_a_finding(self):
        self._stub({("prodhost", "finance.service", "FINANCE_PASSWORD"): False})
        found = analysis.find_stale_locations(_registry([self._unit_key("k1")]))
        self.assertEqual([f["key_id"] for f in found], ["k1"])
        self.assertEqual(found[0]["variable"], "FINANCE_PASSWORD")

    def test_a_unit_that_still_sets_it_is_not(self):
        self._stub({("prodhost", "finance.service", "FINANCE_PASSWORD"): True})
        self.assertEqual(analysis.find_stale_locations(_registry([self._unit_key("k1")])), [])

    def test_unreadable_is_not_evidence(self):
        """None means "could not tell" — an unreachable host must not read as stale."""
        self._stub({("prodhost", "finance.service", "FINANCE_PASSWORD"): None})
        self.assertEqual(analysis.find_stale_locations(_registry([self._unit_key("k1")])), [])

    def test_prose_variables_are_never_probed(self):
        """A location like `data/tasks.db` / "TASKS_LOGIN (…PBKDF2 hash…)" is a note."""
        self._stub({})
        key = _key(
            "k1",
            locations=[
                {
                    "host": "prodhost",
                    "project": "tasks",
                    "path": "data/tasks.db",
                    "variable": "TASKS_LOGIN (operator, PBKDF2 hash in DB)",
                }
            ],
        )
        self.assertEqual(analysis.find_stale_locations(_registry([key])), [])
        self.assertEqual(self.probed, [])

    def test_non_unit_paths_are_not_probed(self):
        """File and scheme paths (Keychain:, 1Password:) are ambiguous — left alone."""
        self._stub({})
        key = _key(
            "k1",
            locations=[
                {
                    "host": "mac",
                    "project": "workspace",
                    "path": "Keychain:restic-mac-backup (b2-key)",
                    "variable": "",
                },
                {
                    "host": "mac",
                    "project": "media",
                    "path": "media/backend/.env",
                    "variable": "DEEPSEEK_API_KEY",
                },
            ],
        )
        self.assertEqual(analysis.find_stale_locations(_registry([key])), [])
        self.assertEqual(self.probed, [], "only systemd units may be probed")

    def test_it_is_reported_by_the_audit(self):
        self._stub({("prodhost", "finance.service", "FINANCE_PASSWORD"): False})
        text, had = analysis.render_audit(_registry([self._unit_key("k1")]))
        self.assertTrue(had)
        self.assertIn("no longer exist", text)
        self.assertIn("FINANCE_PASSWORD", text)


class UnitSetsParsingTest(unittest.TestCase):
    """`_unit_sets` reads a probe through ssh — the boundary, so ssh is what gets stubbed."""

    def setUp(self):
        from conductor import hostenv

        self._real = hostenv.ssh
        self.addCleanup(setattr, hostenv, "ssh", self._real)

    def _answer(self, stdout, returncode=0):
        from conductor import hostenv

        hostenv.ssh = lambda host, remote, timeout=40: hostenv.subprocess.CompletedProcess(
            args=[], returncode=returncode, stdout=stdout, stderr=""
        )

    def test_nonzero_count_is_present(self):
        self._answer("1\n")
        self.assertIs(analysis._unit_sets("prodhost", "finance.service", "FINANCE_DB_KEY"), True)

    def test_zero_count_is_absent(self):
        """`grep -c` prints 0 *and* exits 1 — the count is the signal, not the exit code."""
        self._answer("0\n", returncode=1)
        self.assertIs(analysis._unit_sets("prodhost", "finance.service", "FINANCE_PASSWORD"), False)

    def test_missing_file_is_unknown_not_stale(self):
        self._answer("MISSING\n")
        self.assertIsNone(analysis._unit_sets("prodhost", "gone.service", "ANY"))

    def test_garbage_is_unknown(self):
        self._answer("bash: syntax error\n", returncode=2)
        self.assertIsNone(analysis._unit_sets("prodhost", "finance.service", "ANY"))

    def test_a_non_identifier_variable_is_never_probed(self):
        from conductor import hostenv

        hostenv.ssh = lambda *a, **k: self.fail("must not reach ssh with a non-identifier")
        self.assertIsNone(analysis._unit_sets("prodhost", "finance.service", "TASKS_LOGIN (PBKDF2)"))
        self.assertIsNone(analysis._unit_sets("prodhost", "finance.service", ""))


if __name__ == "__main__":
    unittest.main()
