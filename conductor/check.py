"""Verify-gate entry point — the gate itself lives in `conductor/gate/`.

Kept as a thin shim so `python -m conductor check`, `make check`, and the pre-commit hook
keep working (and `conductor.check.main` stays importable for the command registry).

    python -m conductor check
    make check
"""

from __future__ import annotations

from conductor.gate import CHECKS, Check, main

__all__ = ["CHECKS", "Check", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
