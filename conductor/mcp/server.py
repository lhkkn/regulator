"""Minimal stdlib MCP server — stdio transport (Track D1).

Exposes Conductor's capabilities as MCP tools so any agent (opencode / kimi / claude /
dsh) can call them natively instead of a human copy-pasting reconcile output into chat.

Wire protocol (Model Context Protocol, stdio transport): JSON-RPC 2.0, one message per
line, newline-delimited — spec: "Messages are delimited by newlines, and MUST NOT
contain embedded newlines." The server logs to stderr only; stdout is reserved strictly
for JSON-RPC (the client MUST NOT see anything else there).

Implements only what these tools need: initialize, notifications/initialized, ping,
tools/list, tools/call. Stdlib-first (house rule) — no SDK: on Python 3.14 the MCP SDK's
pydantic/anyio deps are risky, and the protocol surface here is small.

**Version honesty.** `SUPPORTED_PROTOCOL_VERSIONS` lists what this server actually
implements, and `initialize` answers with a version from that list — never one it was
merely asked for. Claiming a revision we do not implement is a silent conformance lie:
the client proceeds assuming semantics the server lacks. Upgrading is its own piece of
work; until then, say what we are.

**Tool descriptions are trusted input.** MCP's security guidance is explicit that tool
descriptions and annotations "should be considered untrusted, unless obtained from a
trusted server." Every description here is composed by this repo, so they are trusted —
but that stops being true the moment a third-party server is loaded alongside this one
in a client. Keep third-party servers separately scoped and never auto-approved.

The tool registry lives in `conductor/mcp/tools.py` — name, description, risk class,
inputSchema AND handler in one entry. This module is transport + dispatch only: it holds
no per-tool knowledge, so a declared tool cannot exist without something to run, and
`conductor.gate` can assert that.

Run:  python3 -m conductor mcp
"""

from __future__ import annotations

import json
import sys
import time

from conductor import __version__
from conductor.mcp.tools import by_name, wire_tools

# Protocol revisions this server actually implements — NEWEST FIRST.
#
# The server speaks the 2024-11-05 request/response shapes: `initialize` carrying the
# full capability set, then tools/list and tools/call. It does NOT implement the later
# revisions' architecture (2025-11-25 sampling / roots / elicitation; 2026-07-28
# stateless self-contained requests, per-request capability negotiation, Extensions).
#
# So it must not *claim* them. Per the spec the server answers with the client's version
# only if it supports it, and otherwise answers with one it does — the client then
# decides whether to continue. Echoing an unsupported version is a false claim of
# conformance, and it fails silently: the client proceeds assuming semantics the server
# does not have.
SUPPORTED_PROTOCOL_VERSIONS = ("2024-11-05",)
PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]  # our newest — the fallback


def _warn(message: str) -> None:
    """Diagnostics go to stderr only — stdout is reserved strictly for JSON-RPC."""
    print(f"regulator: {message}", file=sys.stderr)


def negotiate_protocol(requested: str | None) -> str:
    """Pick the version to answer `initialize` with. Never claims more than we implement."""
    if not requested:
        return PROTOCOL_VERSION
    if requested in SUPPORTED_PROTOCOL_VERSIONS:
        return requested
    _warn(
        f"client requested protocol {requested}; this server implements "
        f"{', '.join(SUPPORTED_PROTOCOL_VERSIONS)} — answering {PROTOCOL_VERSION}. "
        "The client decides whether to continue."
    )
    return PROTOCOL_VERSION


def _content(text: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _rpc_error(code: int, message: str) -> dict:
    return {"code": code, "message": message}


def call_tool(name: str, arguments: dict) -> dict:
    """Run a tool through the registry, wrapped in the MCP content envelope.

    The registry is the ONLY way in. An unregistered name is an error and never
    executes — including a name that exists in the actuator's contract registry but was
    never declared as a tool.
    """
    tool = by_name(name)
    if tool is None:
        return _content(f"unknown tool: {name}", is_error=True)
    text, is_error = tool["handler"](arguments)
    return _content(text, is_error=is_error)


def dispatch(method: str, params: dict) -> dict | None:
    """Return a result dict, an error dict {code,message}, or None (no response)."""
    if method == "initialize":
        return {
            "protocolVersion": negotiate_protocol(params.get("protocolVersion")),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "regulator", "version": __version__},
            "instructions": (
                "Regulator control plane. Most tools are read-only; the actuator adds "
                "write-class tools that are dry-run by default and need an explicit "
                "policy opt-in to apply. "
                "projects_list (inventory) / projects_reconcile (drift check). "
                "worktrees_list (live git worktrees per project — parallel agents; flags a dirty shared tree). "
                "context_build (assemble project context; pass `since` for a refactor-audit diff) / context_state (living state). "
                "keys_list (masked inventory) / keys_audit (shared/rotation findings) / "
                "keys_lookup (masked, or reveal=true → 1Password via a read-only service account, "
                "value written to a temp file — never shown in transcript) / "
                "keys_balances (per-key provider balances — the app-side drain signal). "
                "brief (a workspace digest: spend / drift / agents / keys / flags). "
                "crp_file (file a Consultation Request Pack — a durable question for the human "
                "when you hit ambiguity you must not resolve alone; it outlives your session, "
                "appears at the top of brief, and states `default_action` — what you will do if "
                "never answered. Answering is human-only; you cannot answer your own CRP. "
                "Prefer filing over guessing). "
                "backup (off-machine restic→B2 snapshot age). "
                "health (live AI-stack probes: dead/stale credentials). "
                "stack / machine (workspace + whole-machine maps). "
                "watch (proactive-loop heartbeat + last check results). "
                "trace (recent MCP calls — the observability log). "
                "permissions (risk classes + the enforced policy). "
                "local (the offline/local-model lane). "
                "flow (declarative workflows — the orchestration graph). "
                "fleet_list (the run plane — declared services + live state + drift). "
                "ledger (the actuator's hash-chained action history + integrity). "
                "jobs_list / runs_list / runs_summary (jobs, and run history). "
                "clef_ask (bounded decision questions via Cloudflare Clef on Workers AI; "
                "sensitive — sends state off-machine; opt-in, needs CONDUCTOR_CLEF_ENABLED=1). "
                "Write tools — projects_register / projects_register_drift, projects_archive, "
                "docs_render, keys_retire, docker_prune, "
                "worktree_add / worktree_remove (isolated worktrees for parallel agents), "
                "backup_run (dry-run by default; apply=true requires "
                "`conductor policy --allow-write <tool>`)."
            ),
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": wire_tools()}
    if method == "tools/call":
        name = params.get("name")
        if not name:
            return _rpc_error(-32602, "tools/call requires 'name'")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            args = {}  # a non-object payload is not valid tool arguments
        tool = by_name(name)
        if tool is not None:
            missing = [f for f in tool.get("inputSchema", {}).get("required", []) if f not in args]
            if missing:
                return _rpc_error(-32602, f"tools/call missing required argument(s): {', '.join(missing)}")
        from conductor import permissions, trace

        # Only *known* tools reach the policy layer; an unknown name is refused by
        # call_tool above and mutates nothing. The registry is what makes that true: there
        # is no second dispatch path (the actuator is reached through a tool's handler),
        # so a name absent here cannot execute.
        denial = permissions.check(name, args) if tool is not None else None
        t0 = time.perf_counter()
        if denial:
            trace.record(name, args, ok=False, ms=(time.perf_counter() - t0) * 1000, error=f"denied: {denial}")
            return _content(f"denied by policy: {denial}", is_error=True)
        try:
            result = call_tool(name, args)
        except Exception as exc:  # record the failure, then let handle() shape the error
            trace.record(
                name, args, ok=False, ms=(time.perf_counter() - t0) * 1000, error=f"{type(exc).__name__}: {exc}"
            )
            raise
        out_bytes = sum(len(c.get("text", "")) for c in result.get("content", []) if isinstance(c, dict))
        trace.record(
            name, args, ok=not bool(result.get("isError")), ms=(time.perf_counter() - t0) * 1000, out_bytes=out_bytes
        )
        return result
    if method in ("notifications/initialized", "notifications/cancelled", "notifications/progress"):
        return None  # notification — no response
    return _rpc_error(-32601, f"method not found: {method}")


def handle(msg: dict) -> dict | None:
    """Return the JSON-RPC response, or None (notification / nothing to say)."""
    if not isinstance(msg, dict):
        # A batch array or scalar frame is not an MCP message. Per JSON-RPC 2.0 this
        # is an Invalid Request with a null id — never let it crash the server.
        return {
            "jsonrpc": "2.0",
            "id": None,
            "error": _rpc_error(-32600, "invalid request: expected a JSON-RPC object (batch/scalar not supported)"),
        }
    msg_id = msg.get("id")
    method = msg.get("method")
    if method is None:
        return None
    try:
        result = dispatch(method, msg.get("params") or {})
    except Exception as exc:  # never let one bad call kill the server
        if msg_id is None:
            sys.stderr.write(f"handler error on notification: {exc}\n")
            return None
        return {"jsonrpc": "2.0", "id": msg_id, "error": _rpc_error(-32603, f"internal error: {exc}")}
    if msg_id is None:
        return None  # notification: no response, even if dispatch returned something
    if result is None:
        result = {}
    if isinstance(result, dict) and "code" in result and "message" in result:
        return {"jsonrpc": "2.0", "id": msg_id, "error": result}
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def main() -> int:
    sys.stderr.write("regulator mcp server ready (stdio)\n")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            sys.stderr.write(f"json parse error: {exc}\n")
            continue
        response = handle(msg)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
