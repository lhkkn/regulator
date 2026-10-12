"""The actuator envelope — a mutation as a *capability with a contract*.

Conductor's read tools are sensors. This module is the uniform envelope that turns a
mutation into an actuator: **precondition → act → verify → record → rollback/flag**.

  • precondition — a *read* check; refuse if the world isn't in the expected state, so
    the actuator never acts blind.
  • act — the mutation (only with `apply=True`).
  • verify — a *read* that proves the effect. Mandatory: a failed verify is a failed
    action, never success-with-caveats.
  • record — append the outcome to the hash-chained ledger (who, what, verify, rollback).
  • rollback / flag — reversible actions restore prior state; irreversible ones flag.

Dry-run is the default: `run(contract, args, apply=False)` runs the precondition and
returns the plan, changing nothing. `apply=True` is gated by `conductor.permissions`.

    python -m conductor act                     # list contracts
    python -m conductor act backup.run          # dry-run (default)
    python -m conductor act backup.run --apply
    python -m conductor act projects.register --args '{"project":"foo"}' --apply
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass

from conductor import ledger


@dataclass(frozen=True)
class Contract:
    """A named, reversible-first mutation with its own proof and undo."""

    name: str  # e.g. "backup.run"
    tool: str  # MCP tool name, e.g. "backup_run"
    description: str
    precondition: Callable[[dict], tuple[bool, str]]
    act: Callable[[dict], dict]
    verify: Callable[[dict, dict], tuple[bool, str]]
    rollback: Callable[[dict, dict], None] | None = None
    reversible: bool = True
    rollback_hint: str = ""
    risk: str = "write"

    def plan(self, args: dict) -> str:
        return f"would run {self.name} with {json.dumps(args, sort_keys=True, ensure_ascii=False)}"


def _public(effect: dict | None) -> dict | None:
    """Effect as recorded/returned — keys prefixed `_` are private payload (e.g. the
    prior bytes a rollback needs) and never leave the process or enter the ledger."""
    if not isinstance(effect, dict):
        return effect
    return {k: v for k, v in effect.items() if not str(k).startswith("_")}


def actor(explicit: str | None = None) -> str:
    return explicit or os.environ.get("CONDUCTOR_ACTOR") or "cli"


def run(contract: Contract, args: dict | None = None, *, apply: bool = False, actor_name: str | None = None) -> dict:
    """Run one contract through the envelope. Never raises; returns a result dict."""
    args = dict(args or {})
    who = actor(actor_name)
    try:
        pre_ok, pre_detail = contract.precondition(args)
    except Exception as exc:  # a broken precondition must not crash the caller
        pre_ok, pre_detail = False, f"precondition error: {type(exc).__name__}: {exc}"

    result: dict = {
        "contract": contract.name,
        "tool": contract.tool,
        "actor": who,
        "apply": bool(apply),
        "reversible": contract.reversible,
        "precondition": {"ok": pre_ok, "detail": pre_detail},
        "effect": None,
        "verify": None,
        "outcome": None,
        "rollback": contract.rollback_hint or None,
    }

    def _record(outcome: str, verify=None) -> None:
        ledger.append(
            tool=contract.tool,
            action=contract.name,
            args=args,
            precondition=result["precondition"],
            effect=result["effect"],
            verify=verify,
            outcome=outcome,
            rollback=contract.rollback_hint or None,
            actor=who,
        )

    if not pre_ok:
        result.update(ok=False, outcome="refused")
        if apply:
            _record("refused")
        return result

    if not apply:
        result.update(ok=True, outcome="dry-run", plan=contract.plan(args))
        return result

    try:
        effect = contract.act(args)
    except Exception as exc:
        result["effect"] = {"error": f"{type(exc).__name__}: {exc}"}
        result.update(ok=False, outcome="failed")
        _record("failed")
        return result

    result["effect"] = _public(effect)
    try:
        v_ok, v_detail = contract.verify(args, effect)
    except Exception as exc:
        v_ok, v_detail = False, f"verify error: {type(exc).__name__}: {exc}"
    result["verify"] = {"ok": v_ok, "detail": v_detail}

    if v_ok:
        result.update(ok=True, outcome="verified")
    elif contract.reversible and contract.rollback is not None:
        try:
            contract.rollback(args, effect)
            result.update(ok=False, outcome="rolled_back")
        except Exception as exc:
            result["rollback_error"] = f"{type(exc).__name__}: {exc}"
            result.update(ok=False, outcome="flagged")
    else:
        result.update(ok=False, outcome="flagged")

    _record(result["outcome"], verify=result["verify"])
    return result


def render(result: dict) -> str:
    lines = [
        f"{result.get('contract')} [{result.get('outcome')}] "
        f"actor={result.get('actor')}" + ("" if result.get("apply") else " (dry-run)")
    ]
    pre = result.get("precondition") or {}
    lines.append(f"  precondition: {'ok' if pre.get('ok') else 'REFUSED'} — {pre.get('detail', '')}")
    if result.get("plan"):
        lines.append(f"  plan: {result['plan']}")
    if result.get("effect"):
        lines.append(f"  effect: {json.dumps(result['effect'], ensure_ascii=False)}")
    ver = result.get("verify")
    if ver:
        lines.append(f"  verify: {'ok' if ver.get('ok') else 'FAILED'} — {ver.get('detail', '')}")
    if result.get("rollback") and result.get("outcome") not in ("verified", "dry-run", "refused"):
        lines.append(f"  rollback: {result['rollback']}")
    if result.get("rollback_error"):
        lines.append(f"  rollback FAILED: {result['rollback_error']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    from conductor import contracts

    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0].startswith("-"):
        print(contracts.render_list())
        return 0
    name = argv[0]
    contract = contracts.get(name)
    if contract is None:
        sys.stderr.write(f"unknown contract: {name}\n")
        print(contracts.render_list())
        return 2
    args = {}
    if "--args" in argv:
        try:
            args = json.loads(argv[argv.index("--args") + 1])
        except (IndexError, ValueError) as exc:
            sys.stderr.write(f"--args must be JSON: {exc}\n")
            return 2
    result = run(contract, args, apply="--apply" in argv)
    if "--json" in argv:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render(result))
    return 0 if result.get("ok") or result.get("outcome") in ("refused", "rolled_back") else 1


if __name__ == "__main__":
    raise SystemExit(main())
