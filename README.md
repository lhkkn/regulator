# Regulator

**A control plane for solo, AI-native software operations — one MCP server, typed
tools, one accountable human.**

Regulator consolidates the ops tooling a one-person practice otherwise keeps scattered —
the project registry, API-key inventory, context assembly, token spend, service health,
and off-machine backup — behind a single [MCP](https://modelcontextprotocol.io) server.
Any MCP-speaking agent (opencode, Claude, Kimi, …) can then answer *"what is drifting /
what is deployed where / what is the context here?"* with tool calls instead of asking
you. Stdlib-first, local-first: **your keys and personal data never leave the machine.**

Like the centrifugal governor that holds an engine within its limits, Regulator keeps an
autonomous agent within safe ones — reads are open, writes are governed.

## Install

Regulator is **not on the main PyPI index yet.** Install it from git, or grab the wheel
from the [Releases](https://github.com/lhkkn/regulator/releases) page:

```bash
pipx install git+https://github.com/lhkkn/regulator
```

It is also on TestPyPI (a sandbox — the `--index-url` is required):

```bash
pip install --index-url https://test.pypi.org/simple/ regulator
```

Requires Python 3.12+. No runtime dependencies.

## Quickstart

```bash
regulator init        # scaffold a workspace: registry.toml, fleet.toml, config.toml, policy.json
regulator config      # show every setting, its value, and what it enables
regulator brief       # the dashboard: spend, drift, health, keys, flags
```

Nothing is required to start — an unconfigured install degrades to *unavailable*, never
to an error. Point Regulator at your workspace (or let `init` create one), then plug in
only the integrations you have.

## Wire it into an agent

Add Regulator as a local MCP server in your client's config:

```json
{
  "mcpServers": {
    "regulator": { "command": "regulator", "args": ["mcp"] }
  }
}
```

The server speaks JSON-RPC over stdio, so `python -m conductor mcp` works as a drop-in
command too.

## What you get

One MCP surface (35 tools) across the work an operator actually does:

- **Projects** — inventory the workspace, reconcile the registry against reality, and
  register/archive projects (governed writes, below).
- **Fleet** — the declared services joined with live probes (launchd/systemd/docker/HTTP),
  reporting drift.
- **Keys** — a masked API-key registry with an audit (shared/duplicate, rotation gaps),
  provider balances, and read-only reveal from 1Password.
- **Context** — assemble curated project context and living state for a session.
- **Health & spend** — live AI-stack probes and local token-spend rollups, folded into
  `brief`.
- **Backup** — off-machine backup freshness and a restore drill.
- **Actuator** — a small set of *verified* write actions: each runs
  `precondition → act → verify → record → rollback/flag`, is **dry-run by default**,
  needs explicit per-tool opt-in, and is written to a hash-chained audit ledger.

## Design principles

- **Stdlib-first.** No runtime dependency unless a module truly needs it.
- **Local-first / privacy.** Secrets flow through 1Password and stay on the machine.
- **Read-only before mutating.** Query tools ship long before their write counterparts.
- **Governed writes.** Every write tool is an actuator contract; no ungoverned mutation.
- **Discover, don't hand-maintain.** Read reality (git, services, files); store only what
  can't be discovered.
- **Verify before you claim done.** `regulator check` is the source of truth; its failures
  are the spec.

## Configuration

Machine-specific values live in a single `config.toml` in the per-user app data directory
(on macOS, under `~/Library/Application Support/`), never in the code or the package.
`regulator config` lists every slot — workspace root, registry/fleet paths, production
host, backup repo, and optional integrations (Clef judge, key aliases, watched ports) —
with its value and what enabling it unlocks. Every slot can also be set from an
environment variable, so a machine or a CI runner can point Regulator at its own values.

## Development

```bash
make check     # the verify gate (workspace + network); failures are the spec
make ci        # lint (ruff) + types (mypy) + the self-contained unit suite
```

The Python package keeps the internal codename `conductor` (its module path and data
directory), so `python -m conductor …` and the `regulator` command are the same program.
See [`AGENTS.md`](AGENTS.md) for the working rules and how to build, test, and verify.

## License

MIT — see [`LICENSE`](LICENSE).
