# Regulator — Agent Guide

Regulator is the control plane for an agent-augmented solo practice: it consolidates the
ops tooling a one-person practice keeps scattered (project registry, API-key inventory,
context assembly, token spend, service health, off-machine backup) behind a single
[MCP](https://modelcontextprotocol.io) server. Python, stdlib-first, local-first.

The Python package keeps the internal codename **`conductor`** (its module path and data
directory), so `python -m conductor …` and the `regulator` command are the same program.

## The one rule — verify before you claim done

`make check` (a.k.a. `regulator check` / `python -m conductor check`) is the source of
truth for "is this healthy." **Run it before claiming a task is done.** Its failures *are*
the spec — read them, fix the root cause, re-run. Do not bypass.

## Hard rules

- **Stdlib-first.** No runtime dependency unless a module truly needs it; prefer the
  standard library and existing code before adding anything.
- **Discover, don't hand-maintain.** Read reality (git, services, files); store only what
  can't be discovered.
- **MCP-native.** Capabilities are exposed as MCP tools an agent calls — not just UIs.
- **Read-only before mutating.** Query tools ship long before their write counterparts.
  Every write tool is a governed *actuator contract*
  (`precondition → act → verify → record → rollback/flag`), dry-run by default, and gated
  by `regulator policy --allow-write`. No ungoverned write tool.
- **Single source of truth + generation.** Generated files say so and are never
  hand-edited; the gate fails when they drift.
- **Local-first / privacy.** Secrets flow through the 1Password backend and never sit in
  plaintext in the repo.
- **Simplest thing that fully works.** No speculative abstraction; grow in layers; decide
  for the long term.

## Working here

```bash
make check     # the verify gate (workspace + network); failures are the spec
make lint      # ruff + mypy
make ci        # lint + types + the self-contained unit suite
```

- **Plan non-trivial work** before coding — write it down, then build.
- **Architecture:** the package is split by concern: `projects/` (registry), `fleet/`
  (run plane), `keys/` (masked key registry), `context/`, `tokens/`, `runtime/`, `mcp/`
  (the server), `gate/` (the verify gate), `contracts.py` + `actuator.py` (governed
  writes), `ledger.py` (the tamper-evident action log).

## Layout

| Path | Responsibility |
|---|---|
| `conductor/mcp/` | the MCP server and the tool registry |
| `conductor/gate/` | the verify gate (`make check`) |
| `conductor/actuator.py`, `contracts.py` | the governed-write envelope + the contracts |
| `conductor/ledger.py` | hash-chained action record |
| `conductor/projects/`, `fleet/`, `keys/`, `context/`, `tokens/`, `runtime/` | the read surface |
| `flows/` | declarative workflow DAGs |
| `eval/` | golden-set evaluation of the MCP surface |
