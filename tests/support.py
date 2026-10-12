"""Shared test scaffolding — temp-dir + env redirection, and small fixtures.

Not a test module (it doesn't match `test_*.py`), so `unittest discover` ignores it.
Import it as `from tests.support import EnvTestCase` (the repo root is on `sys.path` via
each test module's own insert).
"""

from __future__ import annotations

import json
import os
import tempfile
import tomllib
import unittest
from pathlib import Path


class EnvTestCase(unittest.TestCase):
    """A TestCase with a throwaway temp dir and leak-free env redirection.

    Subclasses call `super().setUp()` then `self.set_env(NAME=value)`. Every variable is
    restored via `addCleanup`, so a failing test can't leak state into the next one (the
    hand-rolled `_saved` pattern a per-file tearDown used is easy to get wrong).
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    @property
    def tmpdir(self) -> Path:
        return Path(self.tmp.name)

    def set_env(self, **vars: str) -> None:
        for key, value in vars.items():
            previous = os.environ.get(key)
            if previous is None:
                self.addCleanup(os.environ.pop, key, None)
            else:
                self.addCleanup(os.environ.__setitem__, key, previous)
            os.environ[key] = value


def write_registry(path: Path, projects: list[dict]) -> None:
    """Write a minimal `registry.toml` (one `[[project]]` table per dict)."""
    lines: list[str] = []
    for project in projects:
        lines.append("[[project]]")
        for key, value in project.items():
            lines.append(f"{key} = {json.dumps(str(value))}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def read_registry(path: Path) -> list[dict]:
    with path.open("rb") as fh:
        return tomllib.load(fh).get("project", [])
