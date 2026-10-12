"""Unit tests for conductor/registry_notes.py — the prose-rot lint.

`lint_project` takes an explicit project dict, so these are pure: no registry, no
workspace, no filesystem. The rules are narrow on purpose (a false positive on the
fail tier blocks a commit), so the tests pin both directions: what must fail, and
what must NOT.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conductor import registry_notes as rn  # noqa: E402


def proj(notes, name="demo", status="active"):
    return {"name": name, "status": status, "notes": notes}


class FailTierTest(unittest.TestCase):
    def test_no_x_yet_fails(self):
        out = rn.lint_project(proj("A generic fine-tuning system. No training run yet."))
        self.assertTrue(out["fail"])
        self.assertIn("negative present tense", out["fail"][0]["rule"])

    def test_not_a_git_repo_fails(self):
        out = rn.lint_project(proj("Updates: rsync the dir (not a git repo) or add a repo."))
        self.assertTrue(any("not a git repo" in f["excerpt"] for f in out["fail"]))

    def test_asserted_absence_fails(self):
        for text in ("The bridge hasn't been built.", "The rotate tool does not exist.", "nothing deployed yet."):
            with self.subTest(text=text):
                self.assertTrue(rn.lint_project(proj(text))["fail"], text)

    def test_dated_snapshot_fails(self):
        self.assertTrue(rn.lint_project(proj("Verified as of 2026-09-19 by hand."))["fail"])

    def test_volatile_qualifier_fails(self):
        self.assertTrue(rn.lint_project(proj("Currently served from the Mac."))["fail"])

    def test_archived_is_out_of_scope(self):
        """Retired work is out of scope — the sprawl rule applies to live trees."""
        out = rn.lint_project(proj("No training run yet. Not a git repo.", status="archived"))
        self.assertEqual(out["fail"], [])
        self.assertEqual(out["warn"], [])

    def test_vendor_is_out_of_scope(self):
        self.assertEqual(rn.lint_project(proj("No X yet.", status="vendor"))["fail"], [])

    def test_clean_note_passes(self):
        out = rn.lint_project(
            proj(
                "Deployed on prodhost behind cloudflared. The moat is cross-brand calibration, "
                "not the model — see SPEC.md."
            )
        )
        self.assertEqual(out["fail"], [])


class WarnTierTest(unittest.TestCase):
    def test_long_note_warns_and_does_not_fail(self):
        out = rn.lint_project(proj("x" * (rn.NOTES_MAX + 1)))
        self.assertEqual(out["fail"], [])
        self.assertTrue(any("chars (cap" in w["rule"] for w in out["warn"]))

    def test_note_at_cap_does_not_warn(self):
        self.assertEqual(rn.lint_project(proj("x" * rn.NOTES_MAX))["warn"], [])

    def test_count_of_things_that_change_warns(self):
        out = rn.lint_project(proj("Holds 107 expenses and 48 contracts."))
        self.assertEqual(out["fail"], [])
        self.assertTrue(any("count of a thing" in w["rule"] for w in out["warn"]))

    def test_empty_note_is_silent(self):
        self.assertEqual(rn.lint_project(proj(""))["warn"], [])


class ReportAndRenderTest(unittest.TestCase):
    def test_render_flags_only_the_fail_tier(self):
        rows = [
            rn.lint_project(proj("No training run yet.", name="trainer")),
            rn.lint_project(proj("y" * 900, name="wordy")),
            rn.lint_project(proj("Short and true.", name="fine")),
        ]
        _, flags = rn.render(rows)
        self.assertEqual(len(flags), 1)
        self.assertIn("trainer", flags[0])
        self.assertNotIn("wordy", flags[0])  # advisory only

    def test_render_is_clean_when_nothing_is_wrong(self):
        text, flags = rn.render([rn.lint_project(proj("Short and true."))])
        self.assertEqual(flags, [])
        self.assertIn("clean", text)

    def test_report_survives_a_broken_registry(self):
        """A lint must never be the thing that breaks the gate."""
        with mock.patch("conductor.projects.projects", side_effect=RuntimeError("boom")):
            self.assertEqual(rn.report(), [])
