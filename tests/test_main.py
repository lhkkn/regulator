"""Unit tests for conductor/__main__.py — the command dispatcher's argument handling.

The happy paths shell out to real commands (and `check` runs the whole gate), so these
cover the argument-validation branches only. `conductor.gate.scaffold.check_commands`
covers the registry→callable resolution.
"""

import io
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conductor.__main__ import main  # noqa: E402


class DispatcherTest(unittest.TestCase):
    def _run(self, argv):
        err = io.StringIO()
        with redirect_stderr(err):
            rc = main(argv)
        return rc, err.getvalue()

    def test_no_subcommand_prints_usage(self):
        rc, err = self._run(["conductor"])
        self.assertEqual(rc, 2)
        self.assertIn("usage:", err)

    def test_unknown_subcommand_is_rejected(self):
        rc, err = self._run(["conductor", "no-such-command"])
        self.assertEqual(rc, 2)
        self.assertIn("unknown command", err)


if __name__ == "__main__":
    unittest.main()
