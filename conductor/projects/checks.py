"""Shared data layer for the workspace registry — ported from registry/checks.py.

One source of truth for both the reconciler and (later) the dashboard. Every function
returns plain data (dicts/lists): no printing, no exit codes, no HTML.

Port notes (Track A2.1): logic is verbatim from registry/checks.py; only the path math
changed — `ROOT` now comes from find_workspace_root() and `REG` from registry_path()
instead of the script's own location. The original registry/checks.py stays in place
driving the live dashboard until A2.3 swaps it.
"""

from __future__ import annotations

import subprocess
import tomllib
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path

from conductor import config, hostenv
from conductor.projects import find_workspace_root, locate, registry_path


def _root() -> Path:
    return find_workspace_root()


def _reg() -> Path:
    return registry_path()


def _launchagents() -> Path:
    return config.launch_agents_dir()


def load() -> tuple[dict, list]:
    with _reg().open("rb") as fh:
        data = tomllib.load(fh)
    return data.get("meta", {}), data.get("project", [])


def launch_groups() -> list:
    """Browser launch groups from registry.toml (label + urls + startup flag)."""
    with _reg().open("rb") as fh:
        return tomllib.load(fh).get("launch", [])


def _git(d: Path, *a: str) -> str:
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
    are versioned inside the workspace ROOT repo, not as standalone repos."""
    try:
        s = str(d.resolve())
    except OSError:
        return False
    return s.startswith(str(_root() / "work" / "film") + "/")


def _days_since(iso: str):
    try:
        return (date.today() - datetime.strptime(iso, "%Y-%m-%d").date()).days
    except Exception:
        return None


def local_state(name: str) -> dict:
    """Fresh git facts for one project's working tree.

    Tool-layer projects (tools/, film/) are versioned inside the workspace ROOT
    repo — not standalone — so they report git=False (no per-project drift)."""
    d = project_dir(name)
    if not d.exists():
        return {"exists": False, "git": False}
    if not (d / ".git").is_dir():
        if _tool_layer(d):
            return {"exists": True, "git": False, "note": "tracked via workspace root repo"}
        return {"exists": True, "git": False}
    dirty = [x for x in _git(d, "status", "--porcelain").splitlines() if x.strip()]
    last = _git(d, "log", "-1", "--format=%cd", "--date=short")
    return {
        "exists": True,
        "git": True,
        "branch": _git(d, "rev-parse", "--abbrev-ref", "HEAD"),
        "last": last,
        "age": _days_since(last),
        "dirty": len(dirty),
    }


def prod_state(projects: list, host: str) -> dict:
    """One ssh round-trip: systemd active-state + prod branch per deployed repo."""
    svc = [p for p in projects if p.get("service")]
    dirs = [p for p in projects if p.get("deploy") == host]
    remote = []
    for p in svc:
        remote.append(f'echo "SVC {p["name"]} {p["service"]} $(systemctl is-active {p["service"]} 2>/dev/null)"')
    for p in dirs:
        remote.append(
            f'[ -d ~/{p["name"]}/.git ] && echo "BR {p["name"]} '
            f'$(git -C ~/{p["name"]} rev-parse --abbrev-ref HEAD 2>/dev/null)" || true'
        )
    out: dict = {"reachable": False, "service": {}, "branch": {}}
    try:
        r = hostenv.ssh(host, "; ".join(remote), timeout=40)
    except Exception as e:
        out["error"] = str(e)
        return out
    if r.returncode != 0 and not r.stdout:
        out["error"] = r.stderr.strip()[:160]
        return out
    out["reachable"] = True
    for row in r.stdout.splitlines():
        parts = row.split()
        if parts[:1] == ["SVC"] and len(parts) >= 3:
            out["service"][parts[1]] = parts[-1]
        elif parts[:1] == ["BR"] and len(parts) >= 3:
            out["branch"][parts[1]] = parts[2]
    return out


def http_state(url: str, health_path: str = "/", timeout: float = 6) -> dict:
    target = url.rstrip("/") + (health_path or "/")
    try:
        req = urllib.request.Request(target, method="GET", headers={"User-Agent": "workspace-registry"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return {"url": target, "code": r.status, "ok": r.status < 400}
    except urllib.error.HTTPError as e:
        # 401/403 = reachable but auth-protected (still "up"); other 4xx/5xx = down
        protected = e.code in (401, 403)
        return {
            "url": target,
            "code": e.code,
            "ok": protected,
            "protected": protected,
            "error": None if protected else f"HTTP {e.code}",
        }
    except Exception as e:
        return {"url": target, "code": None, "ok": False, "error": f"{type(e).__name__}: {str(e)[:60]}"}


def registry_drift(projects: list) -> list:
    """Drift both ways: dirs on disk missing from the registry, AND registry
    entries whose local directory has vanished (deleted project left dangling)."""
    known = {p["name"] for p in projects}
    issues = []
    for base in ("work/apps", "work/research"):  # registered-project homes (archive is cold)
        pdir = _root() / base
        if not pdir.is_dir():
            continue
        for child in sorted(pdir.iterdir()):
            if child.name.startswith(".") or not child.is_dir():
                continue
            if child.name not in known:
                # A linked worktree is a second working copy of a project that *is*
                # registered — not an unregistered project. Counting it produces a
                # permanent false WARN for every active parallel-agent task.
                from conductor import worktree

                if worktree.is_linked(child):
                    continue
                issues.append(f"{base}/{child.name}: on disk but not in registry.toml")
            elif not any(child.iterdir()):
                issues.append(f"{base}/{child.name}: empty directory")
    # registry -> disk: local/pages projects must have a directory somewhere
    for p in projects:
        if p.get("deploy") in ("local", "github-pages") and not project_dir(p["name"]).exists():
            issues.append(f"{p['name']}: in registry.toml but its directory is gone (deleted?)")
    return issues


def full_report(check_prod: bool = True, check_http: bool = True, self_url: str | None = None) -> dict:
    """Assemble everything into one structure. `level` per project/issue is
    one of: ok | warn | bad."""
    meta, projects = load()
    host = meta.get("production_host") or config.production_host()
    stale_days = meta.get("uncommitted_stale_days", 7)
    prod = prod_state(projects, host) if check_prod else {"reachable": None, "service": {}, "branch": {}}

    # endpoint health, all in parallel (was the main source of slow page loads)
    http_results = {}
    if check_http:
        todo = [
            (p["name"], p["url"], p.get("health_path", "/"))
            for p in projects
            if p.get("url") and not (self_url and p["url"].rstrip("/") == self_url.rstrip("/"))
        ]
        if todo:
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=8) as ex:
                futs = {ex.submit(http_state, u, hp): n for n, u, hp in todo}
                for f in futs:
                    try:
                        http_results[futs[f]] = f.result()
                    except Exception:
                        pass

    rows, issues = [], []
    for p in projects:
        name = p["name"]
        s = local_state(name)
        dep = p.get("deploy", "")
        via = (
            "docker"
            if p.get("docker")
            else "systemd"
            if p.get("service")
            else "pages"
            if dep == "github-pages"
            else "local"
            if (dep == "local" and (p.get("launch_url") or p.get("url")))
            else ""
        )
        row = {
            "name": name,
            "title": p.get("title", ""),
            "status": p.get("status", ""),
            "deploy": dep,
            "url": p.get("url", ""),
            "via": via,
            "launch_url": p.get("launch_url", ""),
            "service": p.get("service", ""),
            "notes": p.get("notes", ""),
            "level": "ok",
            "flags": [],
        }
        row.update(
            {
                "exists": s.get("exists"),
                "git": s.get("git"),
                "branch": s.get("branch"),
                "last": s.get("last"),
                "age": s.get("age"),
                "dirty": s.get("dirty"),
            }
        )

        # uncommitted / no-git flags (skip retired code)
        if p.get("status") not in ("archived", "vendor"):
            if s.get("exists") and not s.get("git"):
                if p.get("status") == "active":
                    row["level"] = "warn"
                    row["flags"].append("not a git repo")
                    issues.append(("warn", f"{name}: active but not a git repo"))
            elif s.get("git") and s.get("dirty"):
                stale = s.get("age") is not None and s["age"] > stale_days
                if stale:
                    row["level"] = "warn"
                    row["flags"].append(f"{s['dirty']} uncommitted, {s['age']}d old")
                    issues.append(("warn", f"{name}: {s['dirty']} uncommitted, {s['age']}d old"))
                else:
                    row["flags"].append(f"{s['dirty']} uncommitted")

        # production service + branch drift
        if p.get("deploy") == host:
            row["prod_service"] = prod["service"].get(name)
            if row.get("prod_service") and row["prod_service"] != "active":
                row["level"] = "bad"
                row["flags"].append(f"service {row['prod_service']}")
                issues.append(("bad", f"{name}: {p.get('service')} = {row['prod_service']}"))
            pb = prod["branch"].get(name)
            row["prod_branch"] = pb
            want = p.get("prod_branch")
            if pb and want and pb != want:
                row["level"] = "warn"
                row["flags"].append(f"prod on {pb}, expected {want}")
                issues.append(("warn", f"{name}: prod on '{pb}', expected '{want}'"))
            if pb and s.get("branch") and s["branch"] != pb:
                row["flags"].append(f"local {s['branch']} != prod {pb}")

        # public endpoint health (skip our own URL to avoid the dashboard
        # recursively HTTP-checking itself while it renders a page)
        if check_http and p.get("url"):
            if self_url and p["url"].rstrip("/") == self_url.rstrip("/"):
                row["health"] = {"url": p["url"], "code": "self", "ok": True}
            else:
                h = http_results.get(name) or http_state(p["url"], p.get("health_path", "/"))
                row["health"] = h
                if not h["ok"]:
                    row["level"] = "bad"
                    issues.append(("bad", f"{name}: {h['url']} -> {h.get('error') or h.get('code')}"))
        rows.append(row)

    for msg in registry_drift(projects):
        issues.append(("warn", msg))

    if check_prod and prod.get("reachable") is False:
        issues.append(("bad", f"cannot reach {host}: {prod.get('error', 'ssh failed')}"))

    counts: dict = {}
    for p in projects:
        counts[p.get("status", "?")] = counts.get(p.get("status", "?"), 0) + 1

    return {
        "host": host,
        "projects": rows,
        "issues": issues,
        "counts": counts,
        "prod_reachable": prod.get("reachable"),
        "checked_prod": check_prod,
        "checked_http": check_http,
    }


# ─────────────────────── operational surface (discovered) ───────────────────────


def _handles():
    """Identifiers -> owner. Project names, their service names, and tools/ dirs."""
    _, projects = load()
    names, svc_owner, status = set(), {}, {}
    for p in projects:
        names.add(p["name"])
        status[p["name"]] = p.get("status")
        if p.get("service"):
            svc_owner[p["service"]] = p["name"]
    tdir = _root() / "tools"
    if tdir.exists():
        for c in tdir.iterdir():
            if c.is_dir() and not c.name.startswith("."):
                names.add(c.name)
                status[c.name] = "tool"
    return names, svc_owner, status


def _attribute(text: str, names) -> str | None:
    hits = [n for n in names if n and n in text]
    return max(hits, key=len) if hits else None


# Vendor / OS unit markers. A systemd unit whose stem contains one of these is not
# the operator's — it is systemd/network/docker plumbing that happened to be a
# regular file. Used to keep discovered inventories to what the workspace runs.
VENDOR_UNIT_MARKERS = (
    "battery",
    "piavpn",
    "snap.",
    "mesa",
    "docker",
    "containerd",
    "ssh",
    "rsyslog",
    "systemd",
    "dbus",
    "network",
    "thermald",
    "wpa",
    "resolved",
    "cron",
    "polkit",
    "udev",
    "getty",
    "apparmor",
    "unattended",
    "cloud-init",
    "multipath",
    "snapd",
)


def is_vendor_unit(unit: str) -> bool:
    """True if a discovered systemd unit is vendor/OS plumbing, not the operator's."""
    stem = unit.replace(".service", "").lower()
    return any(marker in stem for marker in VENDOR_UNIT_MARKERS)


def ops_inventory(host: str | None = None) -> dict:
    """Discover crons/services/launchagents across both machines, attribute each
    to a project, and flag orphans. Nothing here is hand-maintained."""
    meta, _ = load()
    host = host or meta.get("production_host") or config.production_host()
    names, svc_owner, status = _handles()
    crons, services, agents, containers, issues = [], [], [], [], []

    # Enumerate the operator's own units — regular files in /etc/systemd/system
    # (package units are symlinks into /lib, so [ -L ] filters them out). Vendor /
    # OS units that survive that are dropped in Python by is_vendor_unit().
    remote = (
        'echo "###CRON"; crontab -l 2>/dev/null; echo "###SVC"; '
        "for f in /etc/systemd/system/*.service; do "
        '[ -L "$f" ] && continue; '
        'u=$(basename "$f"); echo "$u $(systemctl is-active "$u" 2>/dev/null)"; '
        "done; "
        'echo "###DOCKER"; sudo -n docker ps -a '
        '--format "{{.Names}}|{{.Status}}|{{.Ports}}" 2>/dev/null'
    )
    try:
        r = hostenv.ssh(host, remote, timeout=40)
        section = None
        for ln in r.stdout.splitlines():
            if ln == "###CRON":
                section = "cron"
                continue
            if ln == "###SVC":
                section = "svc"
                continue
            if ln == "###DOCKER":
                section = "docker"
                continue
            s = ln.strip()
            if not s or s.startswith("#"):
                continue
            if section == "cron":
                owner = _attribute(ln, names)
                orphan = owner is None or status.get(owner) == "archived"
                crons.append({"machine": host, "cmd": ln, "owner": owner, "orphan": orphan})
                if orphan:
                    issues.append(("warn", f"cron on {host} not tied to an active project: {ln[:64]}…"))
            elif section == "svc":
                parts = ln.split()
                unit, st = parts[0], (parts[1] if len(parts) > 1 else "?")
                key = unit.replace("cloudflared-", "").replace(".service", "")
                if is_vendor_unit(unit) and unit not in svc_owner and _attribute(key, names) is None:
                    continue
                owner = svc_owner.get(unit) or _attribute(key, names) or key
                orphan = status.get(owner) == "archived"
                services.append({"machine": host, "unit": unit, "state": st, "owner": owner, "orphan": orphan})
                if st not in ("active", "activating") and not unit.endswith("-update.service"):
                    issues.append(("bad", f"{unit} on {host} = {st}"))
                if orphan:
                    issues.append(("warn", f"{unit} serves archived project '{owner}'"))
            elif section == "docker":
                parts = ln.split("|")
                cname = parts[0]
                cstatus = parts[1] if len(parts) > 1 else "?"
                cports = parts[2] if len(parts) > 2 else ""
                owner = _attribute(cname, names) or cname
                up = cstatus.startswith("Up")
                containers.append(
                    {"machine": host, "name": cname, "status": cstatus, "ports": cports, "owner": owner, "up": up}
                )
                if not up:
                    issues.append(("bad", f"docker '{cname}' on {host}: {cstatus}"))
    except Exception as e:
        issues.append(("bad", f"ops: cannot reach {host}: {e}"))

    if _launchagents().exists():
        loaded = set()
        try:
            for ln in hostenv.launchctl_list().splitlines()[1:]:
                cols = ln.split("\t")
                if len(cols) >= 3:
                    loaded.add(cols[2])
        except Exception:
            pass
        for f in sorted(_launchagents().glob("*.plist")):
            # plutil -> json avoids stdlib plistlib (broken pyexpat on this python)
            try:
                pl = hostenv.plist_json(f)
            except Exception:
                pl = None
            if not pl:
                continue
            label = pl.get("Label", f.stem)
            pa = pl.get("ProgramArguments")
            args = " ".join(pa) if isinstance(pa, list) else str(pl.get("Program", ""))
            prefixes = tuple(x for x in config.setting("launchagent_prefixes", "com.conductor.").split(",") if x)
            owned = label.startswith(prefixes)
            try:
                ws = str(_root())
            except Exception:  # noqa: BLE001
                ws = ""
            if not owned and (not ws or ws not in args):
                continue  # only surface agents owned by label prefix or workspace path
            if pl.get("StartCalendarInterval") or pl.get("StartInterval"):
                kind = "scheduled"  # cron-like: fires then exits
            elif pl.get("KeepAlive") or pl.get("RunAtLoad"):
                kind = "always-on"  # service-like: kept running
            else:
                kind = "on-demand"
            agents.append(
                {
                    "machine": "mac",
                    "label": label,
                    "file": f.name,
                    "loaded": label in loaded,
                    "kind": kind,
                    "owner": _attribute(args, names) or _attribute(label, names),
                }
            )
            if label not in loaded:
                issues.append(("warn", f"launchagent {label} installed but not loaded"))

    return {
        "host": host,
        "crons": crons,
        "services": services,
        "agents": agents,
        "containers": containers,
        "issues": issues,
    }
