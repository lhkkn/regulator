"""Scaffold & config checks — the practice files, the registry, the command registry,
the docs guardrails, and the agent definitions (AGENTS.md convention + model ids)."""

from __future__ import annotations

from conductor.gate.runner import Check, _here

# The lifecycle vocabulary. `active` and `archived` were the only two ever used; the
# middle two are what "I want to not work on this" actually needs. See the workspace
# AGENTS.md §Scope for what each one means to an agent.
VALID_STATUS = ("active", "maintenance", "dormant", "archived", "vendor", "empty")
# The other two closed vocabularies. `kind` is the one that was missing: without it,
# `active` reads as "features expected" for an experiment that is explicitly not meant
# to be finished — which is how a learning project becomes a maintenance burden.
VALID_DOMAIN = ("life", "film", "politics", "machine")
VALID_KIND = ("product", "tool", "experiment", "record")


def check_practice_scaffold(c: Check) -> None:
    """Track A1/E1 — the practice scaffolding must stay intact."""
    c.expect(
        _here("AGENTS.md").is_file(),
        "canonical AGENTS.md missing",
        "create Stack/control/AGENTS.md (entry point + reflexes)",
    )
    c.expect(
        _here("CONTEXT.md").is_file(),
        "top-level CONTEXT.md missing",
        "create CONTEXT.md (or run: python3 Stack/harness/context/context.py init)",
    )
    c.expect(
        _here(".context/state.md").is_file(),
        ".context/ not scaffolded",
        "create .context/{brief,architecture,decisions,state,personas}.md",
    )
    claude = _here("CLAUDE.md")
    c.expect(
        claude.is_symlink() and str(claude.readlink()) == "AGENTS.md",
        "CLAUDE.md is not a symlink -> AGENTS.md",
        "ln -sf AGENTS.md CLAUDE.md",
    )


def check_package(c: Check) -> None:
    """The package must be importable and structurally complete."""
    c.expect(
        _here("conductor/__init__.py").is_file(),
        "package conductor/__init__.py missing",
        "create conductor/__init__.py",
    )
    c.expect(
        _here("plans/plan-template.md").is_file(),
        "plan template missing (Track E1)",
        "create plans/plan-template.md",
    )


def check_project_lifecycle(c: Check) -> None:
    """Every project declares a lifecycle state an agent can act on — and the cap holds.

    `status` used to be a label nothing read: only `active` and `archived` were ever used,
    and the middle two (`maintenance`, `dormant`) were documented in registry.toml and
    defined nowhere. A vocabulary with no meaning for an agent changes no behaviour.

    The four states are now contract, and the workspace AGENTS.md states what each one
    means for an agent's *scope*. This check makes the contract enforceable:

      * every value is in the known set (a typo silently reads as "unknown" today)
      * `dormant` carries a `revisit` line — parking without a condition is just forgetting
      * `active` respects `meta.active_cap` — the anti-expansion rule: one archive per add
    """
    try:
        from conductor.projects import load
    except Exception as exc:  # noqa: BLE001
        c.expect(False, "conductor.projects failed to import", str(exc))
        return
    try:
        data = load()
    except Exception as exc:  # noqa: BLE001
        c.expect(False, "registry unreadable", str(exc))
        return

    projects = data.get("project", [])
    for p in projects:
        name = p.get("name", "?")
        status = p.get("status")
        c.expect(
            status in VALID_STATUS,
            f"project {name!r} has unknown status {status!r}",
            f"use one of: {', '.join(VALID_STATUS)}",
        )
        # domain and kind are required on live entries. Archived ones are out of scope
        # for reporting, so a missing value there is not worth a failure.
        if status not in ("archived", "vendor"):
            for field, allowed in (("domain", VALID_DOMAIN), ("kind", VALID_KIND)):
                value = p.get(field)
                c.expect(
                    value in allowed,
                    f"project {name!r} has unknown/missing {field}: {value!r}",
                    f"use one of: {', '.join(allowed)}",
                )
        if status == "dormant":
            c.expect(
                bool(str(p.get("revisit") or "").strip()),
                f"dormant project {name!r} has no revisit condition",
                'add revisit = "when <condition>" — parking without a condition is just forgetting',
            )

    active = sorted(p.get("name", "?") for p in projects if p.get("status") == "active")
    cap = (data.get("meta") or {}).get("active_cap")
    if isinstance(cap, int):
        c.expect(
            len(active) <= cap,
            f"{len(active)} active projects, cap is {cap}: {', '.join(active)}",
            "one archive per addition — retire a project, or raise meta.active_cap in the registry",
        )


def check_projects(c: Check) -> None:
    """Track A1-cont — the registry source of truth must be locatable."""
    from conductor.projects import registry_path

    try:
        path = registry_path()
    except FileNotFoundError as exc:
        c.expect(False, "workspace registry not found", str(exc))
        return
    c.expect(
        path.is_file(),
        f"registry.toml missing at {path}",
        "set CONDUCTOR_REGISTRY_FILE or fix the workspace layout",
    )


def check_commands(c: Check) -> None:
    """The __main__ command registry must resolve to real callables (no dead commands)."""
    import importlib

    from conductor.__main__ import COMMANDS, USAGE

    for name, (module_name, attr, style) in COMMANDS.items():
        try:
            fn = getattr(importlib.import_module(module_name), attr)
        except Exception as exc:  # noqa: BLE001
            c.expect(False, f"command '{name}' → {module_name}.{attr} not importable", str(exc))
            continue
        c.expect(
            callable(fn),
            f"command '{name}' target {module_name}.{attr} is not callable",
            "point the registry entry at a main() callable",
        )
        c.expect(
            style in ("none", "rest", "argv"),
            f"command '{name}' has unknown arg style '{style}'",
            "use none | rest | argv",
        )
    c.expect(
        USAGE.count("|") >= len(COMMANDS) - 1,
        "USAGE line does not list every command",
        "USAGE is generated from COMMANDS in conductor/__main__.py",
    )


def check_mcp_clients(c: Check) -> None:
    """Every MCP server an agent loads must be declared, with a reason.

    Conductor composes its own tool descriptions, so they are trusted. That guarantee
    stops at the process boundary — the same agent config also loads third-party
    servers, and per MCP's security guidance tool descriptions "should be considered
    untrusted, unless obtained from a trusted server."

    Fail tier: a server in a config but not in `conductor/mcp_clients.toml`. Adding one
    becomes a deliberate act, which is the whole control. Note tier: a declared pin that
    the config does not honour (`uvx pkg` fetches whatever the index serves today).
    """
    try:
        from conductor import mcp_clients
    except Exception as exc:  # noqa: BLE001
        c.expect(False, "conductor.mcp_clients failed to import", str(exc))
        return
    try:
        result = mcp_clients.audit()
    except Exception as exc:  # noqa: BLE001
        c.expect(False, "mcp_clients.audit raised (must be defensive)", str(exc))
        return
    for spec in result["undeclared"]:
        c.expect(
            False,
            f"MCP server loaded but undeclared: {spec['harness']}/{spec['name']} ({spec['transport']})",
            "declare it in conductor/mcp_clients.toml with a trust level and a one-line why",
        )
    for spec in result["unpinned"]:
        c.note(
            f"[mcp] {spec['harness']}/{spec['name']} runs unpinned: "
            f"{' '.join(spec['command']) or spec['url']}  →  pin the version "
            "(uvx 'pkg==X.Y.Z') or set pin = false in conductor/mcp_clients.toml"
        )


def check_registry_notes(c: Check) -> None:
    """`notes` is the one registry field with no gate — so it is the one that rots.

    Every structured field is verified against reality (`deploy` probed, `prod_branch`
    read from git, `url` fetched). `notes` is prose nothing reads, and it carries the
    most load-bearing claims in the file — so an agent plans around a world that no
    longer exists. The fail tier is deliberately narrow (negative present-tense
    assertions); everything advisory goes to `c.note()`. See `conductor/registry_notes.py`.
    """
    try:
        from conductor import registry_notes
    except Exception as exc:  # noqa: BLE001
        c.expect(False, "conductor.registry_notes failed to import", str(exc))
        return
    try:
        rows = registry_notes.report()
        text, flags = registry_notes.render(rows)
    except Exception as exc:  # noqa: BLE001
        c.expect(False, "registry_notes.report raised (must be defensive)", str(exc))
        return
    for flag in flags:
        c.expect(
            False,
            flag,
            "state the positive fact, or move it where it is generated (see the header rule in registry/registry.toml)",
        )
    for row in rows:
        for warn in row["warn"][:2]:
            c.note(f"[registry] {row['name']}: {warn['rule']} — {warn['excerpt']}  →  {warn['fix']}")


def check_docs_health(c: Check) -> None:
    """Docs sprawl guardrail — every project's docs tree must have a front door.

    Failures (orphan docs, missing index, archive-in-live-tree) are spec. Counts are
    informational. Reads the registry + filesystem only; never writes, no network.
    """
    try:
        from conductor import docs_health
    except Exception as exc:
        c.expect(False, "conductor.docs_health failed to import", str(exc))
        return
    try:
        rows = docs_health.report()
        _, flags = docs_health.render(rows)
    except Exception as exc:
        c.expect(False, "docs_health.report raised (must be defensive)", str(exc))
        return
    for flag in flags:
        c.expect(
            False,
            flag,
            "index orphan docs in the project's docs/README.md, or move archived material to projects/archive/",
        )


def check_agent_docs(c: Check) -> None:
    """The AGENTS.md convention — a real (non-symlink) CLAUDE.md is drift.

    `AGENTS.md` is canonical; `CLAUDE.md` must be a symlink to it, so opencode (reads
    AGENTS.md) and Claude (reads CLAUDE.md) act on one source. A real CLAUDE.md can
    silently diverge — the very drift the convention exists to prevent. Workspace-wide
    and read-only; archived projects are cold and skipped.
    """
    try:
        from conductor import agent_docs

        failures = agent_docs.report()
    except Exception as exc:  # noqa: BLE001
        c.expect(False, "conductor.agent_docs failed to import", str(exc))
        return
    for failure in failures:
        c.expect(False, failure, "run: git mv CLAUDE.md AGENTS.md && ln -s AGENTS.md CLAUDE.md")


def check_agent_perms(c: Check) -> None:
    """An agent that can run `conductor` must not be able to answer a CRP.

    The CRP's mechanism is the asymmetry — agents file, humans answer. A broad
    `python3 -m conductor *` allow carries `crp answer` with it, so that property sits one
    allowlist edit away from being false, and silently so. Read-only.
    """
    try:
        from conductor import agent_perms

        failures = agent_perms.report()
    except Exception as exc:  # noqa: BLE001
        c.expect(False, "conductor.agent_perms failed to import", str(exc))
        return
    for failure in failures:
        c.expect(False, failure, "add an explicit deny above the broad allow in the frontmatter")


def check_agent_models(c: Check) -> None:
    """Every agent's `model:` must resolve against the configured providers.

    A typo'd provider or model id (e.g. a provider that isn't declared) fails silently
    at runtime — the agent just won't run. This catches it in the gate. Read-only.
    """
    try:
        from conductor import agent_models

        failures = agent_models.report()
    except Exception as exc:  # noqa: BLE001
        c.expect(False, "conductor.agent_models failed to import", str(exc))
        return
    for failure in failures:
        c.expect(False, failure, "fix the agent's `model:` in Stack/agents/definitions/*.md")
