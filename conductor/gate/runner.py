"""Gate runner — the shared machinery every check uses.

`Check` accumulates failures; `ROOT`/`_here` locate the repo; `_record_run` appends the
run to `check-runs.jsonl` for the console's unified "Runs" view. The checks themselves
live in sibling modules grouped by concern; `conductor/gate/__init__.py` assembles them.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _here(rel: str) -> Path:
    return ROOT / rel


class Check:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.notes: list[str] = []

    def expect(self, condition: bool, message: str, fix: str) -> None:
        if not condition:
            self.failures.append(f"{message}\n    fix: {fix}")

    def note(self, message: str) -> None:
        """Record a finding that is worth printing but must not fail the gate.

        The missing tier: before this, a check could only fail or say nothing, so
        anything advisory had to either lie (pass silently) or block commits until it
        was fixed. Advisory findings — a long registry note, a stale-but-not-wrong
        claim — belong here.
        """
        self.notes.append(message)

    def result(self) -> int:
        if self.notes:
            print(f"regulator check: {len(self.notes)} note(s):\n")
            for note in self.notes:
                print(f"  · {note}\n")
        if not self.failures:
            print("regulator check: OK")
            return 0
        print(f"regulator check: {len(self.failures)} failure(s):\n")
        for i, failure in enumerate(self.failures, 1):
            print(f"  {i}. {failure}\n")
        return 1


def record_run(rc: int, t0: float, failures: list[str]) -> None:
    """Append this run to the run log — the console's unified 'Runs' view reads it.

    Best-effort: recording must never affect the gate. `trigger`/`commit` come from
    the environment so the pre-commit hook logs as 'pre-commit' with the sha; a bare
    `conductor check` logs as 'manual'.
    """
    try:
        if os.environ.get("CONDUCTOR_CHECK_NO_RECORD") == "1":
            return  # probes (e.g. the console's health collector) don't count as runs
        from conductor import config

        rec = {
            "ts": round(t0, 3),
            "ms": int((time.time() - t0) * 1000),
            "rc": rc,
            "trigger": os.environ.get("CONDUCTOR_CHECK_TRIGGER") or "manual",
            "commit": os.environ.get("CONDUCTOR_CHECK_COMMIT") or None,
            "failures": [f.splitlines()[0][:120] for f in failures[:10]],
        }
        path = config.state_file("check-runs.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
        lines = path.read_text(encoding="utf-8").splitlines()
        if len(lines) > 250:  # cap growth
            path.write_text("\n".join(lines[-250:]) + "\n", encoding="utf-8")
    except Exception:  # noqa: BLE001 — recording is never allowed to break the gate
        pass
