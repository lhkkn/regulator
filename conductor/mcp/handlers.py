"""One handler per MCP tool — the *behaviour* half of the tool registry.

`tools.py` binds each entry to its handler here and owns the metadata half (name,
description, risk class, inputSchema). Keeping the two in one entry is what lets
`conductor.gate` assert that a declared tool actually has something to run — the check
the old dispatch if-chain made impossible, because handlers were code the gate could
not inspect.

A handler takes the raw argument object and returns `(text, is_error)`. Plain data,
never an MCP envelope, so a handler is testable without the transport and the envelope
is built in exactly one place (`server.call_tool`).

Import policy — preserved deliberately from the pre-registry `call_tool`:
third-party and heavy project imports live *inside* the handler where they used to be
lazy, and stay at module scope where they used to be eager. That keeps import timing
(and the startup cost of `python3 -m conductor mcp`) unchanged by this refactor.
"""

from __future__ import annotations

import json
import re

from conductor import backup as backup_mod
from conductor.brief import build_brief
from conductor.context.api import build_context, project_state
from conductor.health import report as health_report
from conductor.keys.api import audit_keys, list_keys
from conductor.keys.vault import lookup as vault_lookup
from conductor.projects.cli import format_inventory, run_reconcile
from conductor.tokens import balances as balances_mod

_ANSI = re.compile(r"\x1b\[[0-9;]*m")  # agents read plain text; strip terminal color


# --- shared rendering helpers -------------------------------------------------------


def _render_balances() -> str:
    """Human-readable per-key balance table (masked: id/label/suffix only, never the key)."""
    recs = balances_mod.all_balances()
    if not recs:
        return "no registered keys with a supported balance provider."
    lines = []
    for r in recs:
        bal = f"${r['balance']:.2f}" if r["balance"] is not None else "—"
        if not r["ok"]:
            status = f"ERROR ({r['error']})"
        elif r["is_available"] is False:
            status = "NO CREDIT"
        else:
            status = "OK"
        lines.append(
            f"{r['id']:24} {r['provider']:9} {'…' + (r['suffix'] or ''):11} {bal:>9}  {status:20} {r['label'] or ''}"
        )
    s = balances_mod.summary()
    lines.append("")
    lines.append(
        f"total balance: ${s['total_balance']:.2f}  ·  accounts: {s['accounts']}  ·  "
        f"keys: {s['count']}  ·  errors: {s['errors']}"
    )
    lines.append("(balance is account-level — keys on the same account share it)")
    return "\n".join(lines)


def _render_backup() -> str:
    """Human-readable backup-freshness line (never the credentials)."""
    rec = backup_mod.latest()
    if rec.get("ok"):
        line = f"backup OK — latest snapshot {rec['time']} ({rec['age_hours']}h ago)"
    else:
        line = f"backup PROBLEM — {rec.get('error')}"
    d = backup_mod.drill()
    if d.get("ok"):
        line += f"\nrestore drill OK — last verified {d['age_days']}d ago"
    else:
        line += f"\nrestore drill: {d.get('error')}"
    return line


def _contract(tool: str, args: dict) -> tuple[str, bool]:
    """Route a write-class tool through the actuator (dry-run by default).

    The contract registry owns precondition → act → verify → record → rollback; this is
    only the binding from a declared MCP tool to its contract.
    """
    from conductor import contracts

    return contracts.run_tool(tool, args)


# --- read tools ---------------------------------------------------------------------


def projects_list(args: dict) -> tuple[str, bool]:
    return format_inventory(args.get("status")), False


def projects_reconcile(args: dict) -> tuple[str, bool]:
    extra = []
    if args.get("local", True):
        extra.append("--local")
    if args.get("ops"):
        extra.append("--ops")
    code, out = run_reconcile(extra)
    text = _ANSI.sub("", out).rstrip() + f"\n\n[exit {code}]" + (" — drift found" if code else " — all clear")
    return text, False


def worktrees_list(args: dict) -> tuple[str, bool]:
    from conductor import worktree

    return worktree.render(args.get("project")), False


def context_build(args: dict) -> tuple[str, bool]:
    return (
        build_context(
            args.get("project") or "",
            args.get("files"),
            args.get("exclude"),
            args.get("since"),
        ),
        False,
    )


def context_state(args: dict) -> tuple[str, bool]:
    return project_state(args.get("project") or ""), False


def keys_list(args: dict) -> tuple[str, bool]:
    return list_keys(), False


def keys_audit(args: dict) -> tuple[str, bool]:
    return audit_keys(), False


def keys_balances(args: dict) -> tuple[str, bool]:
    return _render_balances(), False


def backup(args: dict) -> tuple[str, bool]:
    return _render_backup(), False


def brief(args: dict) -> tuple[str, bool]:
    return build_brief()["text"], False


def health(args: dict) -> tuple[str, bool]:
    return health_report(), False


def stack(args: dict) -> tuple[str, bool]:
    from conductor.stack import render as render_stack

    return render_stack(), False


def machine(args: dict) -> tuple[str, bool]:
    from conductor.machine import render as render_machine

    return render_machine(), False


def watch(args: dict) -> tuple[str, bool]:
    from conductor.watch import status as watch_status

    return watch_status(), False


def trace(args: dict) -> tuple[str, bool]:
    from conductor import trace as trace_mod

    return trace_mod.render(trace_mod.load(20)), False


def permissions(args: dict) -> tuple[str, bool]:
    from conductor import permissions as permissions_mod

    return permissions_mod.render(), False


def local(args: dict) -> tuple[str, bool]:
    from conductor import local as local_mod

    return local_mod.status(), False


def flow(args: dict) -> tuple[str, bool]:
    from conductor import flows

    return flows.render_list(), False


def jobs_list(args: dict) -> tuple[str, bool]:
    from conductor import runs as runs_mod

    return runs_mod.render_jobs(args.get("owner"), args.get("category"), args.get("group_by")), False


def runs_list(args: dict) -> tuple[str, bool]:
    from conductor import runs as runs_mod

    return runs_mod.render_runs(args.get("job"), int(args.get("limit", 30))), False


def runs_summary(args: dict) -> tuple[str, bool]:
    from conductor import runs as runs_mod

    return json.dumps(runs_mod.summary(), indent=2), False


def fleet_list(args: dict) -> tuple[str, bool]:
    from conductor import fleet

    return fleet.render(probe=not args.get("offline", False)), False


def ledger(args: dict) -> tuple[str, bool]:
    from conductor import ledger as ledger_mod

    return ledger_mod.report(int(args.get("limit", 20))), False


# --- sensitive tools ----------------------------------------------------------------


def keys_lookup(args: dict) -> tuple[str, bool]:
    """Masked by default; `reveal=true` writes the value to a 0600 temp file and returns
    only the path, so the secret never enters the transcript."""
    return (
        vault_lookup(
            args.get("key_id", ""),
            reveal=bool(args.get("reveal", False)),
            mode=args.get("mode", "tmpfile"),
        ),
        False,
    )


def clef_ask(args: dict) -> tuple[str, bool]:
    from conductor import config, judge

    questions = args.get("questions")
    if not isinstance(questions, dict) or not questions:
        return "clef.ask requires a non-empty 'questions' object", True
    try:
        answers = judge.ask(args.get("state"), questions, model=args.get("model") or None)
    except judge.ClefError as exc:
        return f"clef.ask failed: {exc}", True
    model = args.get("model") or config.CLEF_MODEL
    return json.dumps({"model": model, "answers": answers}, indent=2), False


# --- write tools (actuator contracts; dry-run unless apply=true and allow-listed) ----


def backup_run(args: dict) -> tuple[str, bool]:
    return _contract("backup_run", args)


def projects_register(args: dict) -> tuple[str, bool]:
    return _contract("projects_register", args)


def projects_register_drift(args: dict) -> tuple[str, bool]:
    return _contract("projects_register_drift", args)


def projects_archive(args: dict) -> tuple[str, bool]:
    return _contract("projects_archive", args)


def docs_render(args: dict) -> tuple[str, bool]:
    return _contract("docs_render", args)


def keys_retire(args: dict) -> tuple[str, bool]:
    return _contract("keys_retire", args)


def docker_prune(args: dict) -> tuple[str, bool]:
    return _contract("docker_prune", args)


def worktree_add(args: dict) -> tuple[str, bool]:
    return _contract("worktree_add", args)


def worktree_remove(args: dict) -> tuple[str, bool]:
    return _contract("worktree_remove", args)


def crp_file(args: dict) -> tuple[str, bool]:
    """File a Consultation Request Pack — the agent→human question channel.

    Write-class and governed by the `crp.file` contract, so it inherits dry-run, the
    verification step and the hash-chained ledger. Note what is *absent* here: there is no
    `crp_answer` handler. Answering is CLI-only (`conductor crp answer`), so an agent
    cannot resolve its own question.
    """
    return _contract("crp_file", args)
