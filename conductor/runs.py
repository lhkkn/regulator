"""Jobs & runs — the unified execution model (read-only).

A **job** is something that executes: the verify gate (event-triggered) or a scheduled
cron / launch agent (clock-triggered). Each execution is a **run** with a status. This
is the single model both the console UI and agents (via MCP) read — so there's one
source of truth for "what runs, and did it pass?"

Sources are discovered, never hand-maintained:
  * gate    — `check-runs.jsonl`, written by `conductor check`
  * launchd — ~/Library/LaunchAgents plists + `launchctl list` exit status + log mtime
  * cron    — crontab on the prod host + each job's log mtime / tail

Status vocabulary (one everywhere): ok | fail | running | unknown.

    python -m conductor.runs            # text digest
    from conductor.runs import jobs, runs, summary
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import Counter
from pathlib import Path

from conductor import config, hostenv
from conductor.projects import checks, find_workspace_root

GATE_JOB = "verify-gate"


def _launchagents() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


# jobs that exit nonzero to signal *findings* (not failure) — a "reporter" ran fine
_FINDINGS_EXIT = {"com.conductor.watch"}

# dir / token -> project, where the job text doesn't name the project itself.
# Workspace-specific; the operator declares theirs in config.toml [owner_aliases].


def _owner(*texts: str) -> str:
    """Best-effort project attribution from a job's command / log / label."""
    blob = " ".join(t for t in texts if t)
    try:
        names, svc_owner, _ = checks._handles()
    except Exception:  # noqa: BLE001
        names, svc_owner = set(), {}
    hit = checks._attribute(blob, names)
    if hit:
        return hit
    for key, proj in config.value("owner_aliases", {}).items():
        if key in blob:
            return proj
    for svc, proj in svc_owner.items():
        if svc and svc.replace(".service", "") in blob:
            return proj
    return "infra"


def _category(name: str, owner: str) -> str:
    n = (name or "").lower()
    if owner == "conductor" and ("gate" in n or "verify" in n):
        return "verify"
    if re.search(r"backup|restic|drill", n):
        return "backup"
    if re.search(r"send_notification|notify", n):
        return "notify"
    if re.search(r"watch|alert|glance|monitor", n):
        return "monitor"
    if re.search(r"maintenance|logrotate|cleanup", n):
        return "maintenance"
    if re.search(r"refresh|scrape|live\.py|sync|enrich|run_scrap", n):
        return "data"
    return "app"


# ---------------------------------------------------------------- helpers


def _age(ts: float | None) -> str:
    if not ts:
        return "—"
    s = max(0, int(time.time() - ts))
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def _iso(ts: float | None) -> str | None:
    if not ts:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _mtime(path: str | None) -> float | None:
    try:
        return os.stat(path).st_mtime if path else None
    except OSError:
        return None


def _run(
    job_id: str,
    status: str,
    started: float | None,
    *,
    ms: int | None = None,
    detail: str = "",
    commit: str | None = None,
) -> dict:
    return {
        "job": job_id,
        "status": status,
        "started": started,
        "started_iso": _iso(started),
        "ms": ms,
        "detail": detail,
        "commit": commit,
    }


def _la_schedule(pl: dict) -> str | None:
    ci = pl.get("StartCalendarInterval")
    if ci:
        out = []
        for it in ci if isinstance(ci, list) else [ci]:
            if not isinstance(it, dict):
                continue
            h, m = it.get("Hour") or 0, it.get("Minute") or 0
            s = f"at {h:02d}:{m:02d}"
            if it.get("Day") is not None:
                s = f"day {it['Day']} {s}"
            if it.get("Weekday") is not None:
                s = f"wd {it['Weekday']} {s}"
            out.append(s)
        return ", ".join(out) or None
    si = pl.get("StartInterval")
    return f"every {si}s" if si else None


# ---------------------------------------------------------------- gate (event)


def _gate_job(limit: int = 200) -> dict:
    runs: list[dict] = []
    try:
        path = config.state_file("check-runs.jsonl")
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                rc = r.get("rc")
                nf = len(r.get("failures") or [])
                runs.append(
                    _run(
                        GATE_JOB,
                        "ok" if rc == 0 else "fail",
                        r.get("ts"),
                        ms=r.get("ms"),
                        detail=("exit 0" if rc == 0 else f"exit {rc} · {nf} failure(s)"),
                        commit=r.get("commit"),
                    )
                )
    except Exception:  # noqa: BLE001
        pass
    runs.sort(key=lambda r: r.get("started") or 0, reverse=True)
    return {
        "id": GATE_JOB,
        "name": "verify gate",
        "kind": "gate",
        "trigger": "event",
        "schedule": None,
        "owner": "conductor",
        "category": "verify",
        "host": "mac",
        "log": None,
        "runs": runs,
        "last": runs[0] if runs else None,
    }


# ---------------------------------------------------------------- launchd (clock)


def _mac_jobs() -> list[dict]:
    status: dict[str, object] = {}
    try:
        out = hostenv.launchctl_list()
        for ln in out.splitlines()[1:]:
            cols = ln.split("\t")
            if len(cols) >= 3:
                status[cols[2]] = None if cols[1] in ("-", "") else int(cols[1])
    except Exception:  # noqa: BLE001
        pass
    jobs = []
    for f in sorted(_launchagents().glob("*.plist")):
        try:
            pl = hostenv.plist_json(f)
        except Exception:  # noqa: BLE001
            pl = None
        if not isinstance(pl, dict):
            continue
        label = pl.get("Label", f.stem)
        pa = pl.get("ProgramArguments")
        args = " ".join(pa) if isinstance(pa, list) else str(pl.get("Program", ""))
        prefixes = tuple(x for x in config.setting("launchagent_prefixes", "com.conductor.").split(",") if x)
        try:
            ws = str(find_workspace_root())
        except Exception:  # noqa: BLE001
            ws = ""
        if not (label.startswith(prefixes) or (ws and ws in args)):
            continue
        sched = _la_schedule(pl)
        if not sched:
            continue
        logp = pl.get("StandardOutPath") or pl.get("StandardErrorPath")
        code = status.get(label, "?")
        if code == "?":
            st, detail = "unknown", "not loaded"
        elif code == 0:
            st, detail = "ok", "exit 0"
        elif label in _FINDINGS_EXIT:
            st, detail = "ok", "ran · reported findings"  # a reporter's nonzero = findings, not failure
        else:
            st, detail = "fail", f"exit {code}"
        run = _run(label, st, _mtime(logp), detail=detail)
        own = _owner(label, args, logp or "")
        jobs.append(
            {
                "id": label,
                "name": label,
                "kind": "launchagent",
                "trigger": "clock",
                "schedule": sched,
                "owner": own,
                "category": _category(label, own),
                "host": "mac",
                "log": logp,
                "runs": [run],
                "last": run,
            }
        )
    return jobs


# ---------------------------------------------------------------- cron (clock)


def _cron_jobs() -> list[dict]:
    host = config.production_host()
    if not host:
        return []
    try:
        cron = hostenv.ssh(host, "crontab -l 2>/dev/null", timeout=30).stdout
    except Exception:  # noqa: BLE001
        return []
    parsed = []
    for ln in cron.splitlines():
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        fields = s.split()
        if len(fields) < 6:
            continue
        raw = " ".join(fields[5:])
        m = re.search(r">>?\s*(\S+)", raw)
        log = m.group(1) if m else None
        cdm = re.match(r"cd\s+(\S+)\s*&&", raw)
        if log and not log.startswith("/") and cdm:
            log = cdm.group(1).rstrip("/") + "/" + log
        parsed.append({"schedule": " ".join(fields[:5]), "cmd": re.sub(r">>?.*$", "", raw).strip(), "log": log})
    info = ""
    paths = [p["log"] for p in parsed if p["log"]]
    if paths:
        script = "; ".join(f'echo "@@{p}"; stat -c %Y "{p}" 2>/dev/null; tail -n 15 "{p}" 2>/dev/null' for p in paths)
        try:
            info = hostenv.ssh(host, script, timeout=40).stdout
        except Exception:  # noqa: BLE001
            info = ""
    blocks: dict[str, list[str]] = {}
    cur = None
    for ln in info.splitlines():
        if ln.startswith("@@"):
            cur = ln[2:]
            blocks[cur] = []
        elif cur is not None:
            blocks[cur].append(ln)
    failre = (
        r"Traceback \(most recent call last\)|^\s*(FAILED|CRITICAL|Error|error)\b|"
        r"command not found|Permission denied|No such file or directory"
    )
    jobs = []
    for i, p in enumerate(parsed):
        tail = blocks.get(p["log"] or "", [])
        last = float(tail[0].strip()) if tail and tail[0].strip().isdigit() else None
        text = "\n".join(tail[1:])
        if re.search(failre, text, re.M):
            hit = next((ln for ln in tail[1:] if re.search(failre, ln, re.M)), "")
            st, detail = "fail", (hit[:90] or "error in log")
        elif last:
            st, detail = "ok", "log written"
        else:
            st, detail = "unknown", "no log"
        slug = re.sub(r"\W+", "-", (p.get("cmd") or "").split("/")[-1])[:40] or "cron"
        jid = f"{slug}-{i}"
        run = _run(jid, st, last, detail=detail)
        own = _owner(p.get("cmd") or "", p.get("log") or "", host)
        jobs.append(
            {
                "id": jid,
                "name": p["cmd"],
                "kind": "cron",
                "trigger": "clock",
                "schedule": p["schedule"],
                "owner": own,
                "category": _category(p.get("cmd") or "", own),
                "host": host,
                "log": p["log"],
                "runs": [run],
                "last": run,
            }
        )
    return jobs


# ---------------------------------------------------------------- public


def jobs() -> list[dict]:
    """Every job, gate first, each with `last` + its recent `runs`."""
    return [_gate_job()] + _mac_jobs() + _cron_jobs()


def runs(job: str | None = None, limit: int = 200) -> list[dict]:
    """Runs across all jobs (newest first), or one job's runs if `job` is given."""
    if job:
        for j in jobs():
            if j["id"] == job or j["name"] == job:
                return j["runs"][:limit]
        return []
    flat = [r for j in jobs() for r in j["runs"]]
    flat.sort(key=lambda r: r.get("started") or 0, reverse=True)
    return flat[:limit]


def summary() -> dict:
    js = jobs()
    allr = [r for j in js for r in j["runs"]]
    return {
        "jobs": len(js),
        "failing_jobs": [j["name"] for j in js if j["last"] and j["last"]["status"] == "fail"],
        "by_owner": dict(Counter(j["owner"] for j in js)),
        "by_category": dict(Counter(j["category"] for j in js)),
        "by_kind": dict(Counter(j["kind"] for j in js)),
        "by_host": dict(Counter(j["host"] for j in js)),
        "counts": {
            "jobs": len(js),
            "runs": len(allr),
            "ok": sum(1 for r in allr if r["status"] == "ok"),
            "fail": sum(1 for r in allr if r["status"] == "fail"),
            "unknown": sum(1 for r in allr if r["status"] == "unknown"),
            "event": sum(1 for r in allr if r["job"] == GATE_JOB),
            "clock": sum(1 for r in allr if r["job"] != GATE_JOB),
        },
    }


def _job_line(j: dict) -> str:
    last = j["last"] or {}
    st = last.get("status", "unknown")
    mark = {"ok": "ok  ", "fail": "FAIL", "unknown": "?   ", "running": "..  "}.get(st, "?   ")
    return (
        f"  [{mark}] {j['name'][:42]:<42} {j['kind']:<10} "
        f"{(j['schedule'] or j['trigger']):<16} {_age(last.get('started')):<9} "
        f"{last.get('detail', '')}"
    )


def render_jobs(owner: str | None = None, category: str | None = None, group_by: str | None = None) -> str:
    js = jobs()
    if owner:
        js = [j for j in js if j["owner"] == owner]
    if category:
        js = [j for j in js if j["category"] == category]
    if group_by:
        groups: dict = {}
        for j in js:
            groups.setdefault(j.get(group_by) or "—", []).append(j)
        lines = [f"jobs — {len(js)} (grouped by {group_by})"]
        for g in sorted(groups):
            lines.append(f"[{g}] ({len(groups[g])})")
            lines += [_job_line(j) for j in groups[g]]
        return "\n".join(lines)
    lines = [f"jobs — {len(js)} (gate + scheduled)"]
    lines += [_job_line(j) for j in js]
    s = summary()
    if s["failing_jobs"]:
        lines.append("failing: " + ", ".join(s["failing_jobs"]))
    return "\n".join(lines)


def render_runs(job: str | None = None, limit: int = 30) -> str:
    rs = runs(job, limit)
    head = f"runs — {job or 'all'} ({len(rs)})"
    lines = [head]
    for r in rs:
        mark = {"ok": "ok  ", "fail": "FAIL", "unknown": "?   "}.get(r["status"], "?   ")
        dur = f"{r['ms']}ms" if r["ms"] is not None else "—"
        lines.append(f"  [{mark}] {r['job'][:30]:30} {r['started_iso'] or '—':20} {dur:8} {r['detail']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = argv or []
    if argv and argv[0] == "runs":
        print(render_runs(argv[1] if len(argv) > 1 else None))
    else:
        print(render_jobs())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
