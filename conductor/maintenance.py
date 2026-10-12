"""Weekly maintenance — run the safe actuator fixes, push one digest.

The Tier-2 loop: instead of clearing the safe items by hand, this runs the maintenance
**contracts** through the actuator on a schedule and sends a single "fixed X; here's what
needs you" digest to ntfy.

There is one write path now: the fixes are contracts (see `conductor/contracts.py`), each
with a precondition (is the work due?), an act, and a verify. So every autonomous mutation
is verified and recorded in the ledger, not bespoke code here:
  • `docs.render`   — regenerate stale registry docs
  • `keys.retire`   — retire location-less keys marked revoked/orphaned
  • `docker.prune`  — prune dangling Docker images + build cache

Anything touching production, git history, or a judgment call stays report-only and lands
in the digest as "needs you". Defensive: a contract that refuses or fails degrades to a
digest line, never a crash.

    python -m conductor maintenance            # run the pass + push the digest
    python -m conductor maintenance --dry-run  # show the digest, change nothing
"""

from __future__ import annotations

from conductor import actuator, contracts

# The contracts maintenance runs (order is only for readability).
_MAINTENANCE = ("docs.render", "keys.retire", "docker.prune")


def run(dry: bool = False) -> dict:
    """Apply the maintenance contracts (unless dry) and gather the current flags."""
    fixes: list[str] = []
    problems: list[str] = []
    for name in _MAINTENANCE:
        contract = contracts.get(name)
        if contract is None:
            continue
        result = actuator.run(contract, {}, apply=not dry, actor_name="maintenance")
        pre = result["precondition"]
        if dry:
            if pre["ok"]:
                fixes.append(f"would {contract.description}")
        elif result["outcome"] == "verified":
            summary = (result.get("effect") or {}).get("summary")
            if summary:
                fixes.append(summary)
        else:
            problems.append(f"{name}: {result['outcome']} — {pre['detail']}")
    try:
        from conductor.brief import build_brief

        flags = list(build_brief()["flags"]) + problems
    except Exception as exc:  # noqa: BLE001
        flags = [f"brief unavailable: {exc}"] + problems
    return {"fixes": fixes, "flags": flags}


def render_digest(result: dict) -> str:
    lines = []
    if result["fixes"]:
        lines.append("fixed:")
        lines += [f"• {f}" for f in result["fixes"]]
    if result["flags"]:
        lines.append("needs you:")
        lines += [f"• {f}" for f in result["flags"]]
    return "\n".join(lines) or "nothing to do — all clear"


def main(argv: list[str] | None = None) -> int:
    import sys

    argv = list(sys.argv[1:] if argv is None else argv)
    dry = "--dry-run" in argv
    result = run(dry=dry)
    digest = render_digest(result)

    if dry:
        print(digest)
        return 0

    from conductor import alert

    title = f"Regulator maintenance: {len(result['fixes'])} fixed, {len(result['flags'])} flag(s)"
    try:
        sent = alert.push(title, digest, urgent=bool(result["flags"]), tags="wrench")
    except Exception as exc:
        print(f"maintenance: push failed: {exc}")
        return 1
    print(digest)
    print(f"\n[{'pushed' if sent else 'no ntfy topic configured'}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
