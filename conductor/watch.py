"""Always-on watchers — the proactive loop (checks on a schedule, with a heartbeat).

`alert` and `maintenance` push when they run, but nothing guarantees they *do*. This is
the standing watcher: a small registry of cheap, local checks run on an interval, each
with durable state (last result, consecutive failures) and a **heartbeat** so `brief` can
answer "is the loop actually alive?". New/changed findings escalate through `alert`.

Checks today (local, except `server_backup` which probes the production host over ssh):
  backup        latest restic snapshot is recent
  server_backup newest production-host DB snapshot in object storage is recent
  drill         a verified restore drill is recent
  keys          no overdue key rotations
  disk          the home volume has free space
  companies     opt-in app check (companies_db in config.toml): compliance dates fresh
  admin         the ~/admin document tree is present and non-empty

    python -m conductor watch              # run once, print the report
    python -m conductor watch --status     # heartbeat + last results (no run)
    python -m conductor watch --json
    python -m conductor watch --install    # schedule it (LaunchAgent, hourly)
    python -m conductor watch --uninstall
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from conductor import config, launchagent

_LABEL = "com.conductor.watch"
_INTERVAL = config.WATCH_INTERVAL_SECONDS  # seconds between runs
_HEARTBEAT_STALE_H = config.WATCH_STALE_HOURS  # flag the loop if it hasn't run in this long


def _state_path():
    return config.state_file("watchers.json")


# --- checks (each: () -> (ok: bool, detail: str); never raises out) ----------


def _check_backup() -> tuple[bool, str]:
    from conductor.backup import latest

    r = latest()
    if not r["ok"]:
        return False, f"backup: {r['error']}"
    age = r["age_hours"]
    stale = age is None or age > config.BACKUP_STALE_HOURS
    return not stale, f"backup: last snapshot {age}h ago"


def _check_server_backup() -> tuple[bool, str]:
    from conductor.backup import server_sqlite

    r = server_sqlite()
    if r.get("unprobed"):
        return True, f"server_backup: unprobed ({r['error']})"
    if not r["ok"]:
        return False, f"server_backup: {r['error']}"
    age = r["age_hours"]
    stale = age is None or age > config.BACKUP_STALE_HOURS
    # `count` is snapshots retained (databases × the 30-day window), not databases —
    # reporting one as the other read as ~11x the estate it actually protects.
    return not stale, f"server_backup: {r.get('dbs', '?')} DBs, {r['count']} snapshots, newest {age}h ago"


def _check_drill() -> tuple[bool, str]:
    from conductor.backup import drill

    d = drill()
    if not d["ok"]:
        return False, f"restore drill: {d['error']}"
    ok = d["age_days"] is not None and d["age_days"] < config.DRILL_MAX_DAYS
    return ok, f"restore drill: {d['age_days']}d ago"


def _check_keys() -> tuple[bool, str]:
    from conductor.keys.api import inventory

    inv = inventory()
    overdue = [k["id"] for k in inv if k.get("rotate_days") is not None and k["rotate_days"] < 0]
    if overdue:
        return False, f"keys: {len(overdue)} overdue ({', '.join(overdue[:3])})"
    return True, f"keys: {len(inv)} registered, none overdue"


def _check_key_locations() -> tuple[bool, str]:
    """Does the key registry still describe reality?

    The check that would have caught a service env var sitting in the
    registry for weeks after `bff8e9b` deleted the variable. Nothing compared the registry
    to the units it points at, so the source of truth was never checked against anything —
    and a row that reads as true is worse than no row at all.

    Live: it reads systemd units over ssh.
    """
    try:
        from conductor.keys import analysis
        from conductor.keys.api import registry_path

        gone = analysis.find_stale_locations(analysis.load_registry(registry_path()))
    except Exception as exc:  # noqa: BLE001
        return False, f"key_locations: check error: {type(exc).__name__}: {exc}"
    if gone:
        first = gone[0]
        return (
            False,
            f"key_locations: {len(gone)} registered location(s) no longer exist "
            f"(e.g. {first['host']}/{first['path']} no longer sets {first['variable']})",
        )
    return True, "key_locations: every registered unit location still sets its variable"


def _check_disk() -> tuple[bool, str]:
    import shutil

    u = shutil.disk_usage(str(Path.home()))
    free = u.free / u.total if u.total else 1.0
    return free > 0.10, f"disk: {free:.0%} free"


def _check_companies() -> tuple[bool, str]:
    """Companies app: flag overdue compliance dates or stale registry verification.

    Opt-in domain check: registered only when `companies_db` is set in config.toml.
    """
    import sqlite3

    db = Path(config.setting("companies_db"))
    if not db.exists():
        return True, "companies: unprobed (no local db)"
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        rows = con.execute("SELECT slug, doc FROM companies").fetchall()
        con.close()
    except Exception as exc:  # noqa: BLE001
        return False, f"companies: read error: {type(exc).__name__}"
    overdue: list[str] = []
    stale: list[str] = []
    for r in rows:
        try:
            doc = json.loads(r["doc"])
        except TypeError, ValueError:
            continue
        for c in doc.get("compliance", []):
            if isinstance(c, dict) and c.get("due") and str(c["due"])[:10] < today:
                overdue.append(f"{r['slug']}/{c.get('label', '?')}")
        verified = doc.get("verified")
        if not verified:
            stale.append(r["slug"])
            continue
        try:
            age = (datetime.strptime(today, "%Y-%m-%d") - datetime.strptime(str(verified)[:10], "%Y-%m-%d")).days
            if age > 90:
                stale.append(r["slug"])
        except ValueError:
            stale.append(r["slug"])
    if overdue:
        return False, f"companies: {len(overdue)} overdue ({', '.join(overdue[:3])})"
    if stale:
        return False, f"companies: {len(stale)} unverified/stale ({', '.join(stale[:3])})"
    return True, f"companies: {len(rows)} entities, fresh"


def _check_crps() -> tuple[bool, str]:
    """Expire lapsed CRPs, then report the queue.

    A lapsed deadline is a *finding*, not a silent state change: the agent declared what
    it would do if unanswered, the deadline passed, and the human should know the default
    is now in effect. Reporting it as ok would bury exactly the thing CRPs exist to surface.
    """
    try:
        from conductor import crps

        gone = crps.expire_due()
        if gone:
            ids = ", ".join(f"#{r['id']}" for r in gone)
            return False, f"crp: {len(gone)} default(s) fired unanswered ({ids}) — the declared action is now in effect"
        n = crps.open_count()
        return True, f"crp: {n} open, none past deadline" if n else "crp: queue empty"
    except Exception as exc:  # noqa: BLE001
        return False, f"crp: store unreadable ({type(exc).__name__})"


def _check_admin() -> tuple[bool, str]:
    """The admin document tree (`~/admin/`): present and non-empty.

    The records that leave a trace — identity, tax, contracts — and the links into
    them. If this is gone, the console's Documents view is empty and the graph lost
    its evidence.
    """
    root = Path.home() / "admin"
    if not root.is_dir():
        return False, "admin: ~/admin missing"
    docs = [p for p in root.rglob("*") if p.is_file() and not p.name.startswith("_")]
    if not docs:
        return False, "admin: tree present but empty"
    return True, f"admin: {len(docs)} files"


CHECKS = {
    "backup": _check_backup,
    "server_backup": _check_server_backup,
    "drill": _check_drill,
    "keys": _check_keys,
    "key_locations": _check_key_locations,
    "disk": _check_disk,
    "admin": _check_admin,
    "crp": _check_crps,
}

if config.setting("companies_db"):
    CHECKS["companies"] = _check_companies

# Reach the network or a remote host — skipped when a caller needs an offline run.
LIVE_CHECKS = frozenset({"backup", "server_backup", "drill", "key_locations"})


# --- state ------------------------------------------------------------------


def _load() -> dict:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except OSError, ValueError:
        return {}


def _save(state: dict) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def heartbeat() -> dict:
    """How long since the watcher last ran. `{alive, age_hours, updated}`."""
    state = _load()
    when = state.get("updated")
    if not when:
        return {"alive": False, "age_hours": None, "updated": None}
    try:
        age = (datetime.now(UTC) - datetime.fromisoformat(when)).total_seconds() / 3600
    except ValueError:
        return {"alive": False, "age_hours": None, "updated": when}
    return {"alive": age < _HEARTBEAT_STALE_H, "age_hours": round(age, 1), "updated": when}


def failure_counts() -> dict[str, int]:
    """Consecutive failing runs per check, from the last recorded run.

    The distinction this exists for: a check that started failing this morning and one
    that has failed 189 times are the same string but not the same information. Read-only.
    """
    return {name: int(c.get("failures", 0)) for name, c in _load().get("checks", {}).items()}


def is_chronic(name: str) -> bool:
    """Has this check been failing long enough to stop being news?"""
    return failure_counts().get(name, 0) >= config.CHRONIC_AFTER_RUNS


# --- run / escalate ---------------------------------------------------------


def _escalate(findings: list[str], prev_findings: list[str]) -> None:
    """Push findings through `alert`, but only when the set changed (no nagging)."""
    if findings == prev_findings:
        return
    try:
        from conductor import alert

        body = "\n".join(f"• {f}" for f in findings) if findings else "all clear"
        title = f"Conductor watch: {len(findings)} finding(s)" if findings else "Conductor watch: all clear"
        urgent = any(k in f.lower() for f in findings for k in ("backup", "disk"))
        alert.push(title, body, urgent=urgent, tags="eyes")
    except Exception:  # noqa: BLE001 — escalation is best-effort
        pass


def evaluate(names: list[str] | None = None, *, live: bool = True) -> dict[str, tuple[bool, str]]:
    """Run checks and return `{name: (ok, detail)}` — no state written, no escalation.

    This is the single source of "is this healthy?". `brief` and the gate read it instead
    of re-deriving each fact with their own thresholds — which is exactly how `brief` and
    `watch` came to disagree about whether a 40-hour-old backup was stale (`config`'s 36h
    vs a hardcoded 48h). One rule called from several places, not several rules that drift.

    `live=False` skips the checks that reach the network or a remote host, so the verify
    gate stays offline.
    """
    out: dict[str, tuple[bool, str]] = {}
    for name, fn in CHECKS.items():
        if names is not None and name not in names:
            continue
        if not live and name in LIVE_CHECKS:
            out[name] = (True, f"{name}: not probed (offline)")
            continue
        try:
            out[name] = fn()
        except Exception as exc:  # noqa: BLE001 — a broken check is a finding, not a crash
            out[name] = (False, f"{name}: check error: {type(exc).__name__}: {exc}")
    return out


def run_once(escalate: bool = True) -> dict:
    """Run every check, update durable state, escalate changed findings. Never raises."""
    prev = _load()
    prev_checks = prev.get("checks", {})
    prev_findings = prev.get("findings", [])
    now = datetime.now(UTC).isoformat()

    checks: dict = {}
    findings: list[str] = []
    for name, (ok, detail) in evaluate().items():
        failures = 0 if ok else int(prev_checks.get(name, {}).get("failures", 0)) + 1
        checks[name] = {
            "ok": ok,
            "detail": detail,
            "failures": failures,
            "last_ok": now if ok else prev_checks.get(name, {}).get("last_ok"),
        }
        if not ok:
            findings.append(detail)

    _save({"updated": now, "checks": checks, "findings": findings})
    if escalate:
        _escalate(findings, prev_findings)
    return {"updated": now, "checks": checks, "findings": findings}


# --- render -----------------------------------------------------------------


def _mark(ok: bool, failures: int) -> str:
    """ok / FAIL / old — the one place the three states are named, so `render` and
    `status` cannot drift apart on what a chronic failure looks like."""
    if ok:
        return "ok  "
    return "old " if failures >= config.CHRONIC_AFTER_RUNS else "FAIL"


def render(result: dict) -> str:
    findings = result.get("findings", [])
    lines = [f"watch: {len(result.get('checks', {}))} checks, {len(findings)} finding(s)"]
    for c in result.get("checks", {}).values():
        mark = _mark(c["ok"], c.get("failures", 0))
        lines.append(f"  [{mark}] {c['detail']}" + (f"  (x{c['failures']})" if not c["ok"] else ""))
    return "\n".join(lines)


def status() -> str:
    state = _load()
    hb = heartbeat()
    if not state:
        return "watch: never run (install with `conductor watch --install`)"
    age = "never" if hb["age_hours"] is None else f"{hb['age_hours']}h ago"
    lines = [f"watch: last run {age}" + ("" if hb["alive"] else "  ⚠ stale heartbeat")]
    for c in state.get("checks", {}).values():
        lines.append(f"  [{_mark(c.get('ok'), c.get('failures', 0))}] {c.get('detail')}")
    return "\n".join(lines)


# --- scheduling -------------------------------------------------------------


def _plist_text() -> str:
    from conductor.projects import stack_root

    control = stack_root() / "control"
    program = [str(control / ".venv" / "bin" / "python3"), "-m", "conductor", "watch"]
    return launchagent.render(
        _LABEL, program, workdir=str(control), interval=_INTERVAL, log=str(config.logs_dir() / "watch.log")
    )


def install() -> str:
    launchagent.install(_LABEL, _plist_text(), log=str(config.logs_dir() / "watch.log"))
    return f"installed {_LABEL} (every {_INTERVAL // 60} min)"


def uninstall() -> str:
    launchagent.uninstall(_LABEL)
    return f"removed {_LABEL}"


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if "--install" in argv:
        print(install())
        return 0
    if "--uninstall" in argv:
        print(uninstall())
        return 0
    if "--status" in argv:
        print(status())
        return 0

    result = run_once()
    if "--json" in argv:
        print(json.dumps(result, indent=2))
        return 0
    print(render(result))
    return 0 if not result["findings"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
