"""Reconcile the registry against reality — the piece that catches drift.

Ported from registry/reconcile.py (Track A2.1). Path math is via
find_workspace_root()/registry_path(); `checks` is imported from the package (no
sys.path insert). The report is built as **data** (a list of `(tag, message)` lines,
where a `None` tag is a section header) and rendered on demand — the core never prints
and holds no module-global state, so it is safe to call from the MCP server, flows, or
tests without capturing stdout. Checks, per registry.toml:
  1. Local repos with uncommitted work (warns if older than meta.uncommitted_stale_days).
  2. production-host deploys: systemd active? prod git branch == prod_branch? local == prod?
  3. Public URLs: HTTP reachable?
  4. Drift: dirs under projects/ not in the registry; empty dirs.
  5. Worktrees: linked trees (parallel agents); warn on a dirty shared checkout while one is active.

Exit code is non-zero if anything needs attention.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from datetime import date, datetime
from pathlib import Path

from conductor import config, hostenv
from conductor.projects import checks, find_workspace_root, locate, registry_path

OK, WARN, BAD = "  ok  ", " WARN ", " FAIL "
_COLORS = {OK: "\033[32m", WARN: "\033[33m", BAD: "\033[31m"}

# A report is a list of (tag, message): tag in {OK, WARN, BAD}; None = section header.
Report = list[tuple[str | None, str]]


def _root() -> Path:
    return find_workspace_root()


def _reg() -> Path:
    return registry_path()


def git(d: Path, *a: str) -> str:
    try:
        return subprocess.run(["git", "-C", str(d), *a], capture_output=True, text=True, timeout=15).stdout.strip()
    except Exception:
        return ""


def project_dir(name: str) -> Path:
    # Delegates to the single resolver, keeping the legacy "always return a path"
    # contract (callers compare .exists()).
    return locate(name) or (_root() / "work" / "apps" / name)


def _tool_layer(d: Path) -> bool:
    """True if the project dir lives in the tool layer (tools/ or film/) — these
    are versioned inside the workspace root repo, not as standalone repos, so
    per-project git drift checks don't apply."""
    try:
        s = str(d.resolve())
    except OSError:
        return False
    return s.startswith(str(_root() / "work" / "film") + "/")


def days_since(iso: str) -> int | None:
    try:
        return (date.today() - datetime.strptime(iso, "%Y-%m-%d").date()).days
    except Exception:
        return None


def check_local(projects, stale_days, report: Report) -> None:
    report.append((None, "— Local working trees —"))
    for p in projects:
        if p.get("status") in ("archived", "vendor"):
            continue
        d = project_dir(p["name"])
        if not d.exists():
            continue
        if not (d / ".git").is_dir():
            if _tool_layer(d):
                continue  # versioned via the workspace root repo — not a standalone repo
            if p.get("deploy") in ("local", config.production_host()) and p.get("status") == "active":
                report.append((WARN, f"{p['name']}: active but not a git repo — `git init` to protect it"))
            continue
        dirty = [x for x in git(d, "status", "--porcelain").splitlines() if x.strip()]
        if dirty:
            last = git(d, "log", "-1", "--format=%cd", "--date=short")
            age = days_since(last)
            old = age is not None and age > stale_days
            tag = WARN if old else OK
            extra = f" — last commit {age}d ago (>{stale_days})" if old else ""
            report.append((tag, f"{p['name']}: {len(dirty)} uncommitted file(s){extra}"))
        # else: clean, stay quiet


def check_prod(projects, host, report: Report) -> None:
    report.append((None, f"— {host} services & branches —"))
    svc = [p for p in projects if p.get("service")]
    dirs = [p for p in projects if p.get("deploy") == host]
    remote = ["set -e"]
    for p in svc:
        remote.append(f'echo "SVC {p["name"]} {p["service"]} $(systemctl is-active {p["service"]} 2>/dev/null)"')
    for p in dirs:
        remote.append(
            f'[ -d ~/{p["name"]}/.git ] && echo "BR {p["name"]} $(git -C ~/{p["name"]} rev-parse --abbrev-ref HEAD 2>/dev/null)" || true'
        )
    try:
        out = hostenv.ssh(host, "; ".join(remote), timeout=40)
    except Exception as e:
        report.append((BAD, f"cannot ssh {host}: {e}"))
        return
    if out.returncode != 0 and not out.stdout:
        report.append((BAD, f"ssh {host} failed: {out.stderr.strip()[:120]}"))
        return
    prod_branch = {}
    for row in out.stdout.splitlines():
        parts = row.split()
        if parts[:1] == ["SVC"]:
            _, name, service, *rest = parts
            state = rest[0] if rest else "unknown"
            report.append((OK if state == "active" else BAD, f"{name}: {service} = {state}"))
        elif parts[:1] == ["BR"] and len(parts) >= 3:
            prod_branch[parts[1]] = parts[2]
    # branch drift
    for p in dirs:
        pb = prod_branch.get(p["name"])
        if not pb:
            continue
        want = p.get("prod_branch")
        if want and pb != want:
            report.append((WARN, f"{p['name']}: prod on '{pb}' but registry expects '{want}'"))
        ld = project_dir(p["name"])
        if (ld / ".git").is_dir():
            lb = git(ld, "rev-parse", "--abbrev-ref", "HEAD")
            if lb and lb != pb:
                report.append((WARN, f"{p['name']}: local branch '{lb}' != {host} '{pb}'"))


def check_http(projects, report: Report) -> None:
    report.append((None, "— Public endpoints —"))
    for p in projects:
        if not p.get("url"):
            continue
        h = checks.http_state(p["url"], p.get("health_path", "/"))
        label = f"{h['code']} (auth)" if h.get("protected") else (h.get("code") or h.get("error"))
        report.append((OK if h["ok"] else BAD, f"{p['name']}: {h['url']} -> {label}"))


def check_drift(projects, report: Report) -> None:
    report.append((None, "— Registry vs. disk —"))
    drift = checks.registry_drift(projects)
    for msg in drift:
        report.append((WARN, msg))
    if not drift:
        report.append((OK, "registry and disk agree"))


def check_worktrees(projects, report: Report) -> None:
    report.append((None, "— Worktrees (parallel agents) —"))
    from conductor import worktree

    findings = worktree.detect(projects)
    if findings:
        report.extend(findings)
    else:
        report.append((OK, "no linked worktrees — single-writer trees"))


def check_ops(host, report: Report) -> None:
    """Discovered operational surface (crons / services / launchagents)."""
    ops = checks.ops_inventory(host)
    report.append((None, "— Operational surface (discovered) —"))
    for s in ops["services"]:
        good = s["state"] in ("active", "activating") or s["unit"].endswith("-update.service")
        report.append(
            (
                OK if good and not s["orphan"] else (BAD if not good else WARN),
                f"{s['unit']} = {s['state']} ({s['owner']})",
            )
        )
    for c in ops["crons"]:
        report.append((WARN if c["orphan"] else OK, f"cron [{c['owner'] or 'ORPHAN'}]: {c['cmd'][:60]}"))
    for a in ops["agents"]:
        report.append(
            (OK if a["loaded"] else WARN, f"launchagent {a['label']} ({'loaded' if a['loaded'] else 'not loaded'})")
        )
    for c in ops.get("containers", []):
        report.append((OK if c["up"] else BAD, f"docker {c['name']} ({c['status']}) -> {c['owner']}"))


def collect(local: bool = True, ops_flag: bool = False) -> tuple[int, Report]:
    """Run every check and return (issue_count, report data). Never prints."""
    with _reg().open("rb") as fh:
        data = tomllib.load(fh)
    meta = data.get("meta", {})
    projects = data.get("project", [])
    host = meta.get("production_host") or config.production_host()

    report: Report = []
    check_local(projects, meta.get("uncommitted_stale_days", 7), report)
    check_drift(projects, report)
    check_worktrees(projects, report)
    if not local:
        check_prod(projects, host, report)
        check_http(projects, report)
    if ops_flag and not local:
        check_ops(host, report)
    issues = sum(1 for tag, _ in report if tag in (WARN, BAD))
    return issues, report


def _render(report: Report, issues: int, color: bool = False) -> str:
    out = []
    for tag, msg in report:
        if tag is None:
            out.append(f"\n\033[1m{msg}\033[0m" if color else f"\n{msg}")
        elif color:
            out.append(f"{_COLORS[tag]}[{tag}]\033[0m {msg}")
        else:
            out.append(f"[{tag}] {msg}")
    out.append("")
    summary = f"{issues} item(s) need attention." if issues else "All clear."
    if color:
        summary = f"\033[{33 if issues else 32}m{summary}\033[0m"
    out.append(summary)
    return "\n".join(out)


def run(local: bool = True, ops_flag: bool = False) -> tuple[int, str]:
    """Plain-text report for the MCP tool / flows. Returns (exit_code, text)."""
    issues, report = collect(local, ops_flag)
    return (1 if issues else 0), _render(report, issues)


def main(argv: list[str]) -> int:
    issues, report = collect("--local" in argv, "--ops" in argv)
    print(_render(report, issues, color=True))
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
