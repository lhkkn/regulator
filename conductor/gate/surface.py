"""Interface-surface checks — the MCP tool schema, the actuator ledger, and the
contract registry (the invariant that every write tool is governed)."""

from __future__ import annotations

import re

from conductor.gate.runner import Check

# The model-facing function-name contract (dsh-mcp-client): [A-Za-z0-9_-], max 64 chars.
# A client prefixes `mcp__<server>__`; `mcp__conductor__` costs 15, leaving 49 for the
# raw name. We hold ourselves to the stricter lowercase form so the name is stable
# across clients rather than relying on each one's normalization.
_WIRE_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_RAW_NAME_MAX = 49


def check_mcp(c: Check) -> None:
    """Track D1 — the MCP server must import and expose tools."""
    import json

    from conductor import permissions
    from conductor.mcp.tools import TOOLS, wire_tools

    c.expect(
        len(TOOLS) >= 2,
        "MCP server exposes no tools",
        "define tools in conductor/mcp/tools.py TOOLS",
    )
    names = [t.get("name", "") for t in TOOLS]
    c.expect(
        len(names) == len(set(names)),
        f"duplicate MCP tool names: {sorted({n for n in names if names.count(n) > 1})}",
        "tool names are the dispatch key — they must be unique",
    )
    for tool in TOOLS:
        name = tool.get("name", "")
        # The model-facing function-name contract. The client prefixes `mcp__<server>__`
        # (15 chars for `mcp__conductor__`), so the raw name has 64 - 15 = 49 to work with.
        # A name outside this is silently rewritten by the client and given a 12-hex hash
        # suffix, which rides on every tool definition in every request. The old rule here
        # asserted `startswith("conductor.")` — a *style*, not the wire contract, and one
        # that locked in exactly the characters the contract forbids.
        c.expect(
            bool(_WIRE_NAME.match(name)) and len(name) <= _RAW_NAME_MAX,
            f"MCP tool name violates the wire contract (^[a-z][a-z0-9_]*$, <= {_RAW_NAME_MAX} chars): {name}",
            "flat snake_case with no self-prefix — the client adds mcp__conductor__",
        )
        # Binding the name to the handler's own name keeps a rename mechanical: there is
        # one place to change, and the two can never disagree.
        handler_name = getattr(tool.get("handler"), "__name__", None)
        c.expect(
            handler_name == name,
            f"MCP tool name does not match its handler: {name!r} vs handler {handler_name!r}",
            "name the handler in conductor/mcp/handlers.py after the tool",
        )
        c.expect(
            tool.get("risk") in ("read", "sensitive", "write"),
            f"MCP tool missing/invalid risk class: {name}",
            'add "risk": read|sensitive|write in conductor/mcp/tools.py',
        )
        # The check the old dispatch if-chain made impossible: handlers were code, so
        # nothing could assert that a declared tool had something to run. Now that a
        # handler is part of the entry, a tool with no behaviour is a gate failure.
        c.expect(
            callable(tool.get("handler")),
            f"MCP tool has no callable handler: {name}",
            "bind a handler from conductor/mcp/handlers.py in conductor/mcp/tools.py",
        )
    try:
        json.dumps(wire_tools())
    except (TypeError, ValueError) as exc:
        c.expect(
            False,
            f"tools/list is not JSON-serializable: {exc}",
            "a non-serializable key (e.g. a callable) must be projected out by wire_tools()",
        )
    # Every tool must be named in the server instructions the model reads on connect.
    # They documented 30 of 34 before this check existed — and one of the four missing was
    # `projects_archive`, a write-class actuator the model therefore had no map for.
    from conductor.mcp.server import dispatch

    init = dispatch("initialize", {}) or {}
    instructions = init.get("instructions", "")
    unnamed = [n for n in names if not re.search(rf"\b{re.escape(n)}\b", instructions)]
    c.expect(
        not unnamed,
        f"tools missing from the server instructions: {', '.join(unnamed)}",
        "name every tool in the `instructions` block of conductor/mcp/server.py",
    )
    c.expect(
        set(permissions.risk_map()) == set(names),
        "permissions risk_map does not cover the MCP tool surface",
        "derive risk from conductor.mcp.tools in permissions.risk_map()",
    )
    # The other direction of the same agreement. A policy rule naming a tool that no
    # longer exists is a SILENT no-op — which is exactly how a rename drops a `deny`
    # without anything saying so. Reported loudly instead.
    stale = permissions.unknown_policy_names()
    c.expect(
        not stale,
        f"policy names matching no tool (rule is not applying): {', '.join(stale)}",
        "drop the stale entry via `conductor policy --allow/--deny <tool>`",
    )


def check_ledger(c: Check) -> None:
    """The actuator ledger must import and its hash chain must verify (empty = intact)."""
    try:
        from conductor import ledger
    except Exception as exc:
        c.expect(False, "conductor.ledger failed to import", str(exc))
        return
    c.expect(
        all(callable(getattr(ledger, n, None)) for n in ("append", "load", "verify", "render")),
        "ledger surface incomplete (append/load/verify/render missing)",
        "define the ledger surface in conductor/ledger.py",
    )
    try:
        ok, detail = ledger.verify()
    except Exception as exc:
        c.expect(False, "ledger.verify raised (must be defensive)", str(exc))
        return
    c.expect(
        bool(ok),
        f"action-ledger chain is broken: {detail}",
        "inspect with `conductor ledger`; a broken chain means the history was edited",
    )


def check_contracts(c: Check) -> None:
    """Every write tool must be a governed contract — the actuator's core invariant."""
    try:
        from conductor import actuator, contracts
        from conductor.mcp.tools import TOOLS
    except Exception as exc:  # noqa: BLE001
        c.expect(False, "conductor.contracts/actuator failed to import", str(exc))
        return
    for con in contracts.list_contracts():
        c.expect(
            all(callable(getattr(con, n, None)) for n in ("precondition", "act", "verify")),
            f"contract '{con.name}' is missing precondition/act/verify",
            "define the full envelope in conductor/contracts.py",
        )
        c.expect(
            con.risk == "write",
            f"contract '{con.name}' risk is '{con.risk}' (must be write)",
            "write capabilities are risk='write'",
        )
        c.expect(
            not con.reversible or con.rollback is not None,
            f"reversible contract '{con.name}' has no rollback",
            "give reversible actions a rollback, or mark reversible=False",
        )
    write_tools = {t["name"] for t in TOOLS if t.get("risk") == "write"}
    c.expect(
        {con.tool for con in contracts.list_contracts()} == write_tools,
        "write tools and actuator contracts are out of sync",
        "every write tool in mcp/tools.py needs a contract (and vice versa)",
    )
    c.expect(
        callable(actuator.run) and isinstance(contracts.render_list(), str),
        "actuator/contracts surface incomplete",
        "define run() in actuator and render_list() in contracts",
    )
