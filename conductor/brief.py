"""Conductor brief — a read-only workspace digest (the reborn heartbeat).

Composes the existing collectors into one snapshot: today's token spend, workspace
drift, agent-stack readiness, and key-rotation status, plus a `flags` list of the
highest-signal items. Stdlib-only; no LLM call (an agent can summarize the digest if
wanted — the old conductor's Ollama narrative is deliberately dropped).

This is the salvage from the legacy conductor's `heartbeat.py`: same idea (a periodic
rollup of "what's the state of everything"), rebuilt on the new control plane's own
data layer instead of the dead Docker stack's logs/approvals.

Surfaces:
    python3 -m conductor brief          # print the markdown digest
    python3 -m conductor brief --json   # structured {text, flags, summary}
    brief MCP tool            # same text to any agent
"""

from __future__ import annotations

import io
import json
import re
import sys
from datetime import datetime

from conductor import config

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _spend() -> dict:
    """Token spend over the last 24h from the local CLI logs (kimi/claude/opencode).

    Uses a 24h window (not since-midnight) so the headline matches the by-source
    breakdown — a since-midnight figure contradicts the list near day boundaries.
    Defensive.
    """
    try:
        from conductor.tokens import spend

        s = spend.summary(1)
        by_source = s.get("by_source", {}) or {}
        usd = round(sum(v.get("usd", 0.0) for v in by_source.values()), 4)
        return {"usd": usd, "by_source": by_source}
    except Exception:
        return {"usd": 0.0, "by_source": {}}


def _drift() -> tuple[int, str]:
    """Workspace reconcile (local/offline). Returns (exit_code, output). -1 on error."""
    try:
        from conductor.projects.cli import run_reconcile

        code, out = run_reconcile(["--local"])
        return code, out
    except Exception as exc:
        return -1, f"reconcile failed: {exc}"


def _agent_stack() -> dict:
    """Which agent harnesses are present — derived from the probes, not a second list.

    A hardcoded tuple here silently lags `agents()`: that is how `dsh` ended up probed,
    versioned in `harness/configs/` and running as the primary harness, yet still absent
    from the brief. It also mis-reported `ollama` and `lmstudio` — they are servers, so
    they answer with `running`, not `installed`, and `installed`-only logic filed them
    under "missing" on every single run.

    A harness counts as present when it reports `installed` or `running` truthy.
    """
    try:
        from conductor.runtime import probe

        agents = probe.agents()
    except Exception:  # noqa: BLE001
        return {"installed": [], "missing": []}
    installed: list[str] = []
    missing: list[str] = []
    for name, info in agents.items():
        if name == "mcp_tools" or not isinstance(info, dict):
            continue
        if "installed" not in info and "running" not in info:
            continue  # no presence signal — nothing to report either way
        (installed if (info.get("installed") or info.get("running")) else missing).append(name)
    return {"installed": sorted(installed), "missing": sorted(missing)}


def _health_checks(live: bool) -> dict[str, tuple[bool, str]]:
    """The watcher's own verdicts, **read** rather than re-derived.

    `brief` used to compute backup/drill/keys itself, with its own thresholds — which is
    how it came to call a 40-hour-old snapshot stale while `watch` called it fine (36h
    from config vs a hardcoded 48h). Now there is one rule and two readers, so the digest
    and the watcher cannot disagree. `live=False` keeps the verify gate offline.
    """
    try:
        from conductor import watch

        return watch.evaluate(live=live)
    except Exception:  # noqa: BLE001 — a broken check must not break the digest
        return {}


def _failure_counts() -> dict[str, int]:
    """Consecutive failing runs per check — how `brief` tells news from landscape."""
    try:
        from conductor import watch

        return watch.failure_counts()
    except Exception:  # noqa: BLE001
        return {}


def _balances() -> dict:
    """Per-key provider balances (account-level) — the app-side drain signal. Defensive."""
    try:
        from conductor.tokens import balances

        return {"records": balances.all_balances(), "summary": balances.summary()}
    except Exception:
        return {"records": [], "summary": {}}


def _watch() -> dict:
    """Proactive-watcher heartbeat — is the always-on loop alive? Defensive."""
    try:
        from conductor.watch import heartbeat

        return heartbeat()
    except Exception:
        return {"alive": False, "age_hours": None, "updated": None}


def _health(live: bool) -> tuple[list[str], str]:
    """Live AI-stack health (probe verdicts, not registry metadata). Defensive.

    `live=False` skips the subprocess entirely (verify gate must stay offline).
    """
    try:
        from conductor.health import section

        return section(offline=not live)
    except Exception as exc:
        return [], f"- health probe unavailable: {exc}"


def _crps() -> dict:
    """Open Consultation Request Packs — questions an agent filed and could not answer.

    Rendered first, above spend and drift, because a CRP is the only item in this digest
    that is *waiting on the human*. Everything else is state to be aware of; this is a
    queue with someone blocked behind it.
    """
    try:
        from conductor import crps

        return crps.summary()
    except Exception:  # noqa: BLE001 - a broken CRP store must not break the digest
        return {"open": 0, "rows": [], "lapsed": 0, "lapsed_rows": []}


def build_brief(live_health: bool = True) -> dict:
    """Compose the digest. Returns {'text': markdown, 'flags': [str], 'summary': {...}}."""
    spend = _spend()
    code, drift_out = _drift()
    stack = _agent_stack()
    bal = _balances()
    checks = _health_checks(live_health)
    watch = _watch()
    health_flags, health_body = _health(live_health)
    crp = _crps()

    flags: list[str] = []
    if crp["lapsed"]:
        flags.append(f"{crp['lapsed']} CRP default(s) fired unanswered — a declared action is now in effect")
    if crp["open"]:
        flags.append(f"{crp['open']} open CRP(s) awaiting your answer — `conductor crp list`")
    if code > 0:
        flags.append(f"workspace drift (reconcile exit {code}) — uncommitted/unregistered work")
    if spend["usd"] >= config.SPEND_FLAG_USD:
        flags.append(f"high spend (last 24h): ${spend['usd']:.4f}")
    if bal["summary"].get("errors"):
        flags.append(f"{bal['summary']['errors']} provider key balance error(s) — run keys_balances")
    bal_total = bal["summary"].get("total_balance")
    if bal_total is not None and 0 < bal_total < config.BALANCE_FLAG_USD:
        flags.append(f"low provider balance: ${bal_total:.2f}")
    # A policy statement, not a discovery list: these are the harnesses we drive from. If
    # none is present, something is wrong. dsh belongs here — it is the primary harness.
    core = {"opencode", "dsh", "kimi", "claude"}
    if not (core & set(stack["installed"])):
        flags.append(f"no core agent CLI detected ({'/'.join(sorted(core))} all missing)")
    # Health flags come from the checks themselves — one rule, one threshold, and the
    # digest can no longer disagree with the watcher about whether a thing is failing.
    #
    # Except: a check that has failed for a day is no longer news. It is still reported
    # (below, as chronic) but it stops raising a ⚠, because a permanently red signal is
    # one the reader learns to skip past — and that habit then applies to the real ones.
    counts = _failure_counts()
    chronic: list[str] = []
    for name, (ok, detail) in checks.items():
        if ok:
            continue
        runs = counts.get(name, 0)
        if runs >= config.CHRONIC_AFTER_RUNS:
            chronic.append(f"{detail} (failing {runs}x)")
        else:
            flags.append(detail)
    if not watch["alive"]:
        flags.append("proactive watcher heartbeat stale/absent — run `regulator watch --install`")
    flags.extend(health_flags)

    out = io.StringIO()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    out.write(f"# Regulator brief — {now}\n\n")

    if crp["lapsed"]:
        from conductor import crps as _crps_mod

        out.write(f"## ⚠ Default now in effect — {crp['lapsed']} unanswered CRP(s)\n")
        out.write(_crps_mod.render(crp["lapsed_rows"]) + "\n")
        out.write("\n  the deadline passed; the filer's declared default is what happens next\n\n")

    if crp["open"]:
        from conductor import crps as _crps_mod

        out.write(f"## ⏳ Waiting on you — {crp['open']} open CRP(s)\n")
        out.write(_crps_mod.render(crp["rows"]) + "\n")
        out.write("\n  answer: `regulator crp answer <id> --choose <label> [--why ...]`\n\n")

    out.write("## Flags\n")
    if flags:
        for f in flags:
            out.write(f"- ⚠ {f}\n")
    else:
        out.write("- all clear\n")
    out.write("\n")

    out.write("## Spend (last 24h)\n")
    out.write(f"- **${spend['usd']:.4f}**\n")
    by_src = spend["by_source"] or {}
    if by_src:
        for src, data in sorted(by_src.items(), key=lambda kv: -kv[1].get("usd", 0.0)):
            out.write(f"  - {src}: ${data.get('usd', 0.0):.4f} ({data.get('calls', 0)} calls)\n")
    out.write("\n")

    out.write("## Workspace drift\n")
    if code == 0:
        out.write("- all clear\n")
    elif code < 0:
        out.write(f"- {drift_out.strip()}\n")
    else:
        trimmed = "\n".join(_ANSI.sub("", ln) for ln in drift_out.splitlines()[:15])
        out.write(f"- drift found (exit {code}):\n\n```\n{trimmed}\n```\n")
    out.write("\n")

    out.write("## Agent stack\n")
    out.write(f"- installed: {', '.join(stack['installed']) or '—'}\n")
    out.write(f"- missing: {', '.join(stack['missing']) or '—'}\n")
    out.write("\n")

    out.write("## Health\n")
    if not checks:
        out.write("- (checks unavailable)\n")
    for name, (ok, detail) in checks.items():
        # `old` = chronic: still true, no longer news. Reported so it stays visible, marked
        # so it stops competing with the failures that just started.
        mark = "ok  " if ok else ("old " if counts.get(name, 0) >= config.CHRONIC_AFTER_RUNS else "FAIL")
        out.write(f"- {mark} {detail}\n")
    out.write("\n")

    out.write("## Watch\n")
    if watch["alive"]:
        out.write(f"- proactive loop: last run {watch['age_hours']}h ago\n")
    else:
        out.write("- proactive loop: not running — run `regulator watch --install`\n")
    out.write("\n")

    out.write("## Provider balances\n")
    if bal["records"]:
        for r in bal["records"]:
            b = f"${r['balance']:.2f}" if r["balance"] is not None else "—"
            st = "OK" if r["ok"] else f"ERROR ({r['error']})"
            out.write(f"- {r['label'] or r['id']} (…{r['suffix']}): {b} — {st}\n")
        s = bal["summary"]
        out.write(f"- **total ${s.get('total_balance', 0.0):.2f} across {s.get('accounts', '?')} account(s)**\n")
    else:
        out.write("- no supported balance-provider keys\n")
    out.write("\n")

    out.write("## AI health\n")
    out.write(health_body + "\n")
    out.write("\n")

    out.write("## Docs health\n")
    doc_flags: list[str] = []
    try:
        from conductor.docs_health import render, report

        doc_lines, doc_flags = render(report())
        flags.extend(doc_flags)
        out.write("\n".join(doc_lines) + "\n")
    except Exception as exc:
        out.write(f"- docs health unavailable: {exc}\n")
    out.write("\n")

    summary = {
        "drift": code,
        "crp_open": crp["open"],
        "crp_lapsed": crp["lapsed"],
        "spend_usd_24h": round(spend["usd"], 4),
        "provider_balance": bal["summary"].get("total_balance"),
        "watch_alive": watch["alive"],
        "watch_age_hours": watch["age_hours"],
        # The watcher's verdicts, passed through rather than re-derived — a consumer of
        # --json sees the same answers the watcher and the digest do.
        "checks": {name: {"ok": ok, "detail": detail} for name, (ok, detail) in checks.items()},
        "failing_checks": [name for name, (ok, _) in checks.items() if not ok],
        "chronic_checks": [
            name for name, (ok, _) in checks.items() if not ok and counts.get(name, 0) >= config.CHRONIC_AFTER_RUNS
        ],
        "health_issues": len(health_flags),
        "docs_issues": len(doc_flags),
        "agents_installed": stack["installed"],
        "flags": flags,
    }
    return {"text": out.getvalue(), "flags": flags, "summary": summary}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    brief = build_brief()
    if "--json" in argv:
        print(json.dumps(brief, ensure_ascii=False, indent=2))
    else:
        print(brief["text"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
