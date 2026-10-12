"""The lifecycle contract — `status` is a scope instruction, not a label.

Before this, only `active` and `archived` were ever used and the middle two states were
documented in registry.toml and defined nowhere. A vocabulary with no meaning for an
agent changes no behaviour: an agent reads `status` to decide whether to start work, so
each value has to be checkable.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conductor.gate import scaffold  # noqa: E402
from conductor.gate.runner import Check  # noqa: E402


def run(projects, cap=None, fill=True):
    """Run the check against a synthetic registry; return the failures.

    Live entries need `domain` and `kind`, so fill them unless a test is exercising
    those fields specifically.
    """
    filled = []
    for p in projects:
        q = dict(p)
        if fill and q.get("status") not in ("archived", "vendor"):
            q.setdefault("domain", "machine")
            q.setdefault("kind", "tool")
        filled.append(q)
    meta = {} if cap is None else {"active_cap": cap}
    c = Check()
    with mock.patch("conductor.projects.load", return_value={"project": filled, "meta": meta}):
        scaffold.check_project_lifecycle(c)
    return c.failures


class StatusVocabularyTest(unittest.TestCase):
    def test_every_known_status_passes(self):
        for status in scaffold.VALID_STATUS:
            with self.subTest(status=status):
                extra = {"revisit": "when the condition is met"} if status == "dormant" else {}
                self.assertEqual(run([{"name": "x", "status": status, **extra}]), [])

    def test_unknown_status_fails(self):
        """A typo currently reads as 'unknown' and silently escapes every scope rule."""
        failures = run([{"name": "x", "status": "frozen"}])
        self.assertEqual(len(failures), 1)
        self.assertIn("unknown status", failures[0])

    def test_missing_status_fails(self):
        self.assertTrue(run([{"name": "x"}]))


class DormantTest(unittest.TestCase):
    def test_dormant_without_a_revisit_condition_fails(self):
        """Parking without a condition is just forgetting."""
        failures = run([{"name": "x", "status": "dormant"}])
        self.assertEqual(len(failures), 1)
        self.assertIn("no revisit condition", failures[0])

    def test_dormant_with_a_blank_revisit_fails(self):
        self.assertTrue(run([{"name": "x", "status": "dormant", "revisit": "   "}]))

    def test_dormant_with_a_condition_passes(self):
        self.assertEqual(run([{"name": "x", "status": "dormant", "revisit": "an org exists to serve"}]), [])

    def test_only_dormant_requires_a_revisit(self):
        for status in ("active", "maintenance", "archived"):
            with self.subTest(status=status):
                self.assertEqual(run([{"name": "x", "status": status}]), [])


class ActiveCapTest(unittest.TestCase):
    def test_at_cap_passes(self):
        projects = [{"name": f"p{i}", "status": "active"} for i in range(12)]
        self.assertEqual(run(projects, cap=12), [])

    def test_over_cap_fails_and_names_the_projects(self):
        projects = [{"name": f"p{i}", "status": "active"} for i in range(13)]
        failures = run(projects, cap=12)
        self.assertEqual(len(failures), 1)
        self.assertIn("cap is 12", failures[0])
        self.assertIn("p12", failures[0])

    def test_non_active_statuses_do_not_count(self):
        projects = [{"name": f"p{i}", "status": "maintenance"} for i in range(30)]
        self.assertEqual(run(projects, cap=12), [])

    def test_no_cap_declared_means_no_cap_enforced(self):
        projects = [{"name": f"p{i}", "status": "active"} for i in range(40)]
        self.assertEqual(run(projects), [])

    def test_a_non_integer_cap_is_ignored_not_crashed(self):
        projects = [{"name": f"p{i}", "status": "active"} for i in range(40)]
        self.assertEqual(run(projects, cap="twelve"), [])


class RegistryRobustnessTest(unittest.TestCase):
    def test_unreadable_registry_is_one_failure_not_a_crash(self):
        c = Check()
        with mock.patch("conductor.projects.load", side_effect=RuntimeError("boom")):
            scaffold.check_project_lifecycle(c)
        self.assertEqual(len(c.failures), 1)
        self.assertIn("registry unreadable", c.failures[0])


class DomainAndKindTest(unittest.TestCase):
    """The two vocabularies that say *what a thing is*, not what state it is in."""

    def test_unknown_domain_fails(self):
        f = run([{"name": "x", "status": "active", "domain": "hobbies", "kind": "tool"}])
        self.assertEqual(len(f), 1)
        self.assertIn("unknown/missing domain", f[0])

    def test_unknown_kind_fails(self):
        f = run([{"name": "x", "status": "active", "domain": "life", "kind": "prod"}])
        self.assertEqual(len(f), 1)
        self.assertIn("unknown/missing kind", f[0])

    def test_missing_both_fails_twice(self):
        """Named `frozen` in an earlier draft: a live entry must say what it IS."""
        f = run([{"name": "x", "status": "active"}], fill=False)
        self.assertEqual(len(f), 2)

    def test_every_domain_and_kind_passes(self):
        for dom in scaffold.VALID_DOMAIN:
            for kind in scaffold.VALID_KIND:
                with self.subTest(domain=dom, kind=kind):
                    self.assertEqual(run([{"name": "x", "status": "active", "domain": dom, "kind": kind}]), [])

    def test_archived_entries_need_neither(self):
        """Archived is out of scope for reporting — not worth a failure."""
        self.assertEqual(run([{"name": "x", "status": "archived"}]), [])

    def test_experiment_is_a_kind_not_a_status(self):
        """The whole point: `active` + `experiment` means "expected to stay unfinished"."""
        self.assertEqual(
            run([{"name": "x", "status": "active", "domain": "politics", "kind": "experiment"}]),
            [],
        )
