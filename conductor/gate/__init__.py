"""The Conductor verify gate, split by concern.

The source of truth for "is Conductor healthy." Run before claiming any task done; its
failures *are* the spec (AGENTS.md §"The one rule"). `conductor/check.py` re-exports this
module, so `python -m conductor check`, `make check`, and the pre-commit hook are
unchanged.

Checks are grouped:
  * `scaffold` — practice files, registry, commands, docs guardrails, agent definitions
  * `surface`  — the MCP tool schema, the actuator ledger, the contract registry
  * `modules`  — each internal module's importable surface (keys/tokens/runtime/…)
  * `quality`  — static analysis (ruff + mypy) and the unit-test suite
"""

from __future__ import annotations

import time

from conductor.gate import modules, quality, scaffold, surface
from conductor.gate.runner import ROOT, Check, _here, record_run

CHECKS = [
    scaffold.check_practice_scaffold,
    scaffold.check_package,
    scaffold.check_projects,
    scaffold.check_project_lifecycle,
    surface.check_mcp,
    surface.check_ledger,
    surface.check_contracts,
    scaffold.check_commands,
    modules.check_keys,
    modules.check_tokens,
    modules.check_runtime,
    modules.check_harnesses,
    modules.check_brief,
    modules.check_health,
    modules.check_fleet,
    modules.check_eval,
    scaffold.check_docs_health,
    scaffold.check_registry_notes,
    scaffold.check_mcp_clients,
    scaffold.check_agent_docs,
    scaffold.check_agent_models,
    scaffold.check_agent_perms,
    quality.check_lint,
    quality.check_tests,
]


def main() -> int:
    t0 = time.time()
    c = Check()
    for fn in CHECKS:
        fn(c)
    rc = c.result()
    record_run(rc, t0, c.failures)
    return rc


__all__ = ["CHECKS", "Check", "ROOT", "_here", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
