"""Flows — declarative workflows the stack runs as a small directed graph.

A recurring job ("run the full health pass", "start a session") is a set of steps with
dependencies, not a shell script. This makes that explicit: a flow is a JSON DAG of
steps — each with an `action`, optional `needs` (edges), and a `when` condition — run in
dependency order, with per-step ok/fail captured and fed into later steps' conditions.

It's the orchestration layer above the individual tools: the same actions an agent calls
by hand, wired into a repeatable graph. Flow files live in the repo's `flows/*.json`.

    python -m conductor flow                  # list flows
    python -m conductor flow audit            # run one (drift, keys, backup, local, eval → digest)
    python -m conductor flow start            # session-start pass
    python -m conductor flow --graph audit    # render the DAG (mermaid)
    python -m conductor flow audit --json
    python -m conductor flow fix-drift --apply   # write steps run (only if allow-listed)

Write steps (backup.run, projects.register, projects.register-drift, projects.archive,
docs.render, keys.retire, docker.prune) default to dry-run; `--apply` runs them, and a
write whose tool is not policy-allow-listed or whose precondition isn't due is
refused/skipped, not failed.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _flows_dir() -> Path:
    """Where the flow JSON lives. `CONDUCTOR_FLOWS` wins; then a checkout's `<repo>/flows`;
    then the copy bundled inside the installed package (`conductor/flows/`)."""
    override = os.environ.get("CONDUCTOR_FLOWS")
    if override:
        return Path(override)
    repo = Path(__file__).resolve().parents[1] / "flows"
    if repo.is_dir():
        return repo
    return Path(__file__).resolve().parent / "flows"


FLOWS_DIR = _flows_dir()


# --- actions: () -> (ok, text) ----------------------------------------------


def _a_reconcile() -> tuple[bool, str]:
    from conductor.projects.cli import run_reconcile

    code, out = run_reconcile(["--local"])
    return code == 0, out.strip()


def _a_keys() -> tuple[bool, str]:
    from conductor.keys import analysis
    from conductor.keys.api import audit_keys, registry_path

    reg = analysis.load_registry(registry_path())
    ok = not analysis.find_past_rotation(reg) and not analysis.find_missing_rotation(reg)
    return ok, audit_keys().strip()


def _a_backup() -> tuple[bool, str]:
    from conductor import backup

    rec = backup.latest()
    d = backup.drill()
    txt = "\n".join(
        filter(
            None,
            [
                f"snapshot: {rec['age_hours']}h ago" if rec["ok"] else f"snapshot: {rec['error']}",
                f"drill: {d['age_days']}d ago" if d["ok"] else f"drill: {d['error']}",
            ],
        )
    )
    return bool(rec["ok"]) and bool(d["ok"]), txt


def _a_local() -> tuple[bool, str]:
    from conductor import local

    return local.server_up(), local.status().strip()


def _a_eval() -> tuple[bool, str]:
    from conductor.eval import harness

    ok, report = harness.run()
    return ok, report.strip()


def _a_watch() -> tuple[bool, str]:
    from conductor import watch

    r = watch.run_once(escalate=False)
    return not r["findings"], watch.render(r)


def _a_brief() -> tuple[bool, str]:
    from conductor.brief import build_brief

    b = build_brief(live_health=False)
    return not b["flags"], b["text"].strip()


def _a_stack() -> tuple[bool, str]:
    from conductor.stack import render

    return True, render().strip()


def _a_machine() -> tuple[bool, str]:
    from conductor.machine import render

    return True, render().strip()


ACTIONS = {
    "reconcile": _a_reconcile,
    "keys.audit": _a_keys,
    "backup": _a_backup,
    "local": _a_local,
    "eval": _a_eval,
    "watch": _a_watch,
    "brief": _a_brief,
    "stack": _a_stack,
    "machine": _a_machine,
}

# Write actions dispatch through the actuator contracts. They default to dry-run; a
# flow that would apply them refuses any write whose tool is not policy-allow-listed.
WRITE_TOOLS = {
    "backup.run": "backup_run",
    "projects.register": "projects_register",
    "projects.register-drift": "projects_register_drift",
    "projects.archive": "projects_archive",
    "docs.render": "docs_render",
    "keys.retire": "keys_retire",
    "docker.prune": "docker_prune",
}


# --- flow loading -----------------------------------------------------------


def load_flow(name: str) -> dict | None:
    path = FLOWS_DIR / f"{name}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def list_flows() -> list[dict]:
    out = []
    if FLOWS_DIR.is_dir():
        for p in sorted(FLOWS_DIR.glob("*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                out.append(
                    {
                        "name": data.get("name", p.stem),
                        "description": data.get("description", ""),
                        "steps": len(data.get("steps", [])),
                    }
                )
            except (OSError, ValueError):
                continue
    return out


# --- graph ------------------------------------------------------------------


def _order(steps: list[dict]) -> list[dict]:
    """Topologically order steps so every `needs` runs first."""
    by_id = {s["id"]: s for s in steps}
    seen: set[str] = set()
    ordered: list[dict] = []

    def visit(sid: str, stack: set[str]) -> None:
        if sid in seen or sid in stack:
            return
        stack.add(sid)
        for dep in by_id.get(sid, {}).get("needs", []):
            visit(dep, stack)
        seen.add(sid)
        ordered.append(by_id[sid])

    for s in steps:
        visit(s["id"], set())
    return ordered


def _when_ok(expr: str, results: dict) -> bool:
    if expr in ("always", "", None):
        return True
    if expr == "any_fail":
        return any(not r["ok"] for r in results.values())
    if expr == "all_ok":
        return all(r["ok"] for r in results.values()) if results else True
    if expr.endswith("_ok"):
        r = results.get(expr[:-3])
        return bool(r and r["ok"])
    if expr.endswith("_fail"):
        r = results.get(expr[:-5])
        return bool(r and not r["ok"])
    return True


# --- run --------------------------------------------------------------------


def run(name: str, dry: bool = False, apply: bool = False) -> dict:
    flow = load_flow(name)
    if not flow:
        return {"name": name, "error": f"unknown flow: {name}"}
    steps = _order(flow.get("steps", []))

    if dry:
        return {
            "name": name,
            "dry": True,
            "plan": [
                {
                    "id": s["id"],
                    "action": s.get("action"),
                    "needs": s.get("needs", []),
                    "when": s.get("when", "always"),
                    "args": s.get("args", {}),
                    "write": s.get("action") in WRITE_TOOLS,
                }
                for s in steps
            ],
        }

    results: dict = {}
    for step in steps:
        sid = step["id"]
        action = step.get("action")
        step_args = dict(step.get("args") or {})
        if not _when_ok(step.get("when", "always"), results):
            results[sid] = {"ok": True, "text": f"(skipped — when {step['when']})", "action": action, "skipped": True}
            continue
        if action in WRITE_TOOLS:
            tool = WRITE_TOOLS[action]
            if apply:
                from conductor import permissions

                denial = permissions.check(tool, {**step_args, "apply": True})
                if denial:
                    results[sid] = {
                        "ok": False,
                        "text": f"refused by policy: {denial}",
                        "action": action,
                        "write": True,
                    }
                    if step.get("on_fail", "continue") == "stop":
                        break
                    continue
            from conductor import actuator, contracts

            result = contracts.execute(tool, {**step_args, "apply": apply})
            if result.get("outcome") == "refused":
                # The precondition said the work isn't due — a skip, not a failure.
                results[sid] = {
                    "ok": True,
                    "text": f"(skipped — not due: {result['precondition']['detail']})",
                    "action": action,
                    "write": True,
                    "skipped": True,
                }
                continue
            results[sid] = {
                "ok": bool(result.get("ok")),
                "text": actuator.render(result),
                "action": action,
                "write": True,
                "applied": apply,
            }
            if not result.get("ok") and step.get("on_fail", "continue") == "stop":
                break
            continue
        if action not in ACTIONS:
            results[sid] = {"ok": False, "text": f"unknown action: {action}", "action": action}
            continue
        try:
            ok, text = ACTIONS[action]()
        except Exception as exc:  # noqa: BLE001
            ok, text = False, f"{type(exc).__name__}: {exc}"
        results[sid] = {"ok": ok, "text": text, "action": action}
        if not ok and step.get("on_fail", "continue") == "stop":
            break
    return {"name": name, "description": flow.get("description", ""), "results": results}


def graph(name: str) -> str:
    flow = load_flow(name)
    if not flow:
        return f"unknown flow: {name}"
    lines = ["graph TD", f"  %% flow: {name}"]
    for s in flow.get("steps", []):
        label = f"{s['id']}: {s.get('action')}"
        cond = s.get("when")
        if cond and cond != "always":
            label += f"?{cond}"
        lines.append(f'  {s["id"]}["{label}"]')
    for s in flow.get("steps", []):
        for dep in s.get("needs", []):
            lines.append(f"  {dep} --> {s['id']}")
    return "\n".join(lines)


def render(result: dict) -> str:
    if "error" in result:
        return f"flow: {result['error']}"
    if result.get("dry"):
        lines = [f"flow {result['name']} (plan, {len(result['plan'])} steps):"]
        for s in result["plan"]:
            needs = f" ← {', '.join(s['needs'])}" if s["needs"] else ""
            tag = " [write]" if s.get("write") else ""
            arg = f" {s['args']}" if s.get("args") else ""
            lines.append(f"  - {s['id']} [{s['action']}]{tag}{arg}{needs}")
        return "\n".join(lines)
    lines = [f"flow {result['name']} — {result.get('description', '')}"]
    fails = 0
    for sid, r in result["results"].items():
        if r.get("skipped"):
            mark = "skip"
        elif r["ok"]:
            mark = "ok  "
        else:
            mark = "FAIL"
            fails += 1
        suffix = ""
        if r.get("write"):
            suffix = " (write: applied)" if r.get("applied") else " (write: dry-run)"
        lines.append(f"  [{mark}] {sid} ({r['action']}){suffix}")
    lines.append(f"  → {len(result['results'])} steps, {fails} failing")
    return "\n".join(lines)


def render_list() -> str:
    flows = list_flows()
    if not flows:
        return "flows: none defined"
    lines = ["flows:"]
    for f in flows:
        lines.append(f"  {f['name']:10} ({f['steps']} steps) — {f['description']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(render_list())
        return 0
    if argv[0] == "--graph":
        print(graph(argv[1] if len(argv) > 1 else ""))
        return 0
    name = argv[0]
    result = run(name, dry="--dry-run" in argv, apply="--apply" in argv)
    if "--json" in argv:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    print(render(result))
    if "results" in result:
        for sid, r in result["results"].items():
            if not r["ok"] and not r.get("skipped"):
                body = r["text"] if len(r["text"]) < 800 else r["text"][:800] + " …"
                print(f"\n--- {sid} ({r['action']}) ---\n{body}")
    return 0 if "error" not in result else 1


if __name__ == "__main__":
    raise SystemExit(main())
