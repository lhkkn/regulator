"""Quality checks — static analysis (ruff + mypy) and the unit-test suite."""

from __future__ import annotations

import sys
from pathlib import Path

from conductor.gate.runner import ROOT, Check, _here


def check_lint(c: Check) -> None:
    """Static analysis — ruff (lint) + mypy (types), when the dev tools are installed.

    Both are optional, matching the house rule (stdlib-first: no runtime deps). If a tool
    is present, findings fail the gate; if neither is present, the gate skips with a note.
    Config lives in pyproject.toml; install with
    `uv pip install --python .venv/bin/python3 ruff mypy`.
    """
    import subprocess

    from conductor import proc

    def _bin(name: str) -> str | None:
        return proc.find(
            name,
            str(ROOT / ".venv" / "bin" / name),
            f"/opt/homebrew/bin/{name}",
            f"/usr/local/bin/{name}",
            str(Path.home() / ".local" / "bin" / name),
        )

    ran = False
    for label, tool, argv in (
        ("ruff", "ruff", ["check", "conductor", "tests"]),
        # `make lint` runs format-check too; the gate must cover what the gate's own
        # documented command covers, or a formatting drift passes `check` and fails `lint`.
        ("ruff format", "ruff", ["format", "--check", "conductor", "tests"]),
        ("mypy", "mypy", ["conductor"]),
    ):
        exe = _bin(tool)
        if not exe:
            continue
        ran = True
        try:
            r = subprocess.run([exe, *argv], cwd=str(ROOT), capture_output=True, text=True, timeout=180)
        except Exception as exc:  # noqa: BLE001
            c.expect(False, f"{label} failed to run", str(exc))
            continue
        if r.returncode != 0:
            tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-12:])
            c.expect(False, f"{label} reported issues", (tail or "") + "\n    run: make lint")
    if not ran:
        sys.stderr.write("check_lint: ruff/mypy not installed — skipped (`make lint`)\n")


def check_tests(c: Check) -> None:
    """Track tests/ — the stdlib unittest suite must pass (zero-dep: no pytest).

    Covers the pure logic the gate can't reach: key-registry analysis, spend
    parsers (on injected temp corpora), brief composition, registry grouping.
    """
    import io
    import unittest

    loader = unittest.TestLoader()
    suite = loader.discover(str(_here("tests")), pattern="test_*.py")
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
    if not result.wasSuccessful():
        details = [f"{t.id()}: {m}" for t, m in result.failures + result.errors]
        c.expect(
            False,
            f"unit tests failing ({len(details)} failure(s))",
            "\n".join(details[:6]) or "run: python3 -m unittest discover -s tests -v",
        )
