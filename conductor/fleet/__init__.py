"""Fleet — the run plane: the declared service registry + a live join.

The build plane (`conductor.projects`) answers "is the work correct to build?".
This module answers the other question: "what is running, and is it alive?"

The declared set has one home per service:

  * `fleet.toml` (`Stack/ops/fleet/fleet.toml`) — the service-only services: the
    operator tools and daemons whose subject is
    the machine, plus any service a project does not declare in its own block.
  * `registry.toml` project `service` / `port` / `url` fields — a project's runtime.

Nothing here is discovered state. `list()` joins the declared set against live
probes (launchctl, systemd, docker, HTTP) at read time and reports drift — a
declared service that is down, absent, or unreachable.

    python3 -m conductor fleet              # declared vs. live, with drift
    python3 -m conductor fleet --offline    # the declared set only (no probes)
    python3 -m conductor fleet --json       # machine-readable
"""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from conductor import config, hostenv
from conductor.projects import checks, find_workspace_root
from conductor.projects import load as load_registry

PLANES = ("operator", "domain", "infra")
_PLANE_RANK = {p: i for i, p in enumerate(PLANES)}


def fleet_path() -> Path:
    """The declared-service file: `CONDUCTOR_FLEET_FILE` env > `fleet_file` config > the
    workspace default `<workspace>/fleet.toml`."""
    env = config.setting("fleet_file")
    if env:
        return Path(env).expanduser()
    try:
        ws = find_workspace_root()
    except FileNotFoundError:
        return config.app_dir() / "fleet.toml"
    return ws / "fleet.toml"


def load() -> list[dict]:
    """The `[[service]]` blocks from fleet.toml (empty if the file is absent)."""
    path = fleet_path()
    if not path.is_file():
        return []
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return []
    return [s for s in data.get("service", []) if s.get("name")]


def _from_registry(host: str) -> list[dict]:
    """A registry project with a `service` field is a declared domain service."""
    try:
        projects = load_registry().get("project", [])
    except OSError:
        return []  # no workspace — no registry-backed services
    out: list[dict] = []
    for p in projects:
        if p.get("status") == "archived" or not p.get("service"):
            continue
        out.append(
            {
                "name": p["name"],
                "plane": "domain",
                "runtime": "systemd",
                "host": host,
                "unit": p["service"],
                "bind": f"127.0.0.1:{p['port']}" if p.get("port") else None,
                "tunnel": None,
                "url": p.get("url"),
                "auth": "own",
                "project": p["name"],
                "purpose": p.get("title", ""),
                "cost": "none",
            }
        )
    return out


def _default_host() -> str:
    """Registry meta > config.toml > "mac" (a local-only install hosts services locally)."""
    try:
        meta = load_registry().get("meta", {})
    except OSError:
        meta = {}
    return meta.get("production_host") or config.production_host() or "mac"


def declared(host: str | None = None) -> list[dict]:
    """The declared service set: fleet.toml + a project's own registry runtime."""
    if host is None:
        host = _default_host()
    rows: list[dict] = []
    for raw in load():
        s = dict(raw)
        s.setdefault("plane", "operator")
        s.setdefault("host", host)
        s.setdefault("auth", "none")
        s.setdefault("cost", "none")
        rows.append(s)
    rows.extend(_from_registry(host))
    rows.sort(key=lambda s: (_PLANE_RANK.get(str(s.get("plane") or ""), 9), str(s.get("name", ""))))
    return rows


# ─────────────────────────────── live probes ───────────────────────────────


def _launchctl() -> dict[str, str]:
    """launchd label -> up|down, from the local launchctl table."""
    out: dict[str, str] = {}
    for ln in hostenv.launchctl_list().splitlines():
        cols = ln.split("\t")
        if len(cols) < 3 or cols[0] == "PID":
            continue
        pid, label = cols[0], cols[2]
        out[label] = "up" if pid not in ("-", "0", "") else "down"
    return out


def _local_docker() -> dict[str, str]:
    """Local docker container -> up|down (defensive; empty if docker is absent)."""
    out: dict[str, str] = {}
    try:
        r = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}} {{.Status}}"], capture_output=True, text=True, timeout=8
        )
    except (OSError, subprocess.TimeoutExpired):
        return out
    for ln in r.stdout.splitlines():
        parts = ln.split(None, 1)
        if len(parts) == 2:
            out[parts[0]] = "up" if parts[1].startswith("Up") else "down"
    return out


def _remote(rows: list[dict]) -> dict:
    """One ssh: declared unit states + the full running set (for undeclared detection).

    Returns `{"systemd": {unit: state}, "docker": {name: state}, "running": [units],
    "reachable": bool}`. On any failure the maps are empty and reachable is False, so
    every remote service degrades to "unknown" instead of a false "down".
    """
    host = next(
        (
            s.get("host")
            for s in rows
            if s.get("runtime") in ("systemd", "docker") and s.get("host") and s.get("host") != "mac"
        ),
        None,
    )
    units = [s["unit"] for s in rows if s.get("runtime") == "systemd" and s.get("unit") and s.get("host") == host]
    containers = [s["unit"] for s in rows if s.get("runtime") == "docker" and s.get("unit") and s.get("host") == host]
    empty = {"systemd": {}, "docker": {}, "running": [], "reachable": False}
    if not host or not (units or containers):
        return {"systemd": {}, "docker": {}, "running": [], "reachable": True}
    remote = "; ".join(
        [
            "echo __DECL__",
            *[f'echo "{u} $(systemctl is-active {u} 2>/dev/null)"' for u in units],
            "echo __DOCKER__",
            'sudo -n docker ps --format "{{.Names}} {{.Status}}" 2>/dev/null',
            "echo __RUNNING__",
            "systemctl list-units --type=service --state=running --no-legend --plain 2>/dev/null | awk '{print $1}'",
        ]
    )
    r = hostenv.ssh(host, remote, timeout=40)
    if "__DECL__" not in r.stdout:
        return empty
    out: dict[str, Any] = {"systemd": {}, "docker": {}, "running": [], "reachable": True}
    section = None
    for ln in r.stdout.splitlines():
        s = ln.strip()
        if s in ("__DECL__", "__DOCKER__", "__RUNNING__"):
            section = s
            continue
        if not s:
            continue
        if section == "__DECL__":
            parts = s.split(None, 1)
            if len(parts) == 2:
                out["systemd"][parts[0]] = "up" if parts[1].strip() == "active" else "down"
        elif section == "__DOCKER__":
            parts = s.split(None, 1)
            if len(parts) == 2:
                out["docker"][parts[0]] = "up" if parts[1].startswith("Up") else "down"
        elif section == "__RUNNING__":
            out["running"].append(s)
    return out


def _health(rows: list[dict]) -> None:
    """HTTP-probe every declared url in parallel; auth-protected still counts as up."""
    todo = [r for r in rows if r.get("url")]
    if not todo:
        return
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(checks.http_state, r["url"], r.get("health_path", "/")): r for r in todo}
        for fut, r in futs.items():
            try:
                r["health"] = fut.result()
            except Exception:  # noqa: BLE001
                r["health"] = {"url": r["url"], "ok": False, "error": "probe failed"}


def _attributed(text: str, names: set[str]) -> str | None:
    hits = [n for n in names if n and n in text]
    return max(hits, key=len) if hits else None


def _undeclared(rows: list[dict], live: dict, host: str) -> list[dict]:
    """Running remote units/containers with no declared service (the drift signal).

    Kept to what is clearly the workspace's: a unit/container is only flagged if it
    is attributable to a registered project or a declared service name. OS/vendor
    units and cloudflared tunnels (modeled as a service's `tunnel` field) are excluded,
    as is a sidecar attributable to an already-declared service.
    """
    known = {r.get("unit") for r in rows if r.get("unit")}
    known |= {r.get("tunnel") for r in rows if r.get("tunnel")}
    declared_names = {str(r.get("name")) for r in rows if r.get("declared", True)}
    project_names = {p.get("name", "") for p in load_registry().get("project", [])} - {""}
    out: list[dict] = []
    for unit in live.get("running", []):
        stem = unit.replace(".service", "")
        if unit in known or "cloudflared" in stem or checks.is_vendor_unit(unit):
            continue
        if _attributed(stem, project_names) is None and _attributed(stem, declared_names) is None:
            continue
        out.append(
            {
                "name": stem,
                "plane": "infra",
                "runtime": "systemd",
                "host": host,
                "machine": host,
                "declared": False,
                "state": "up",
                "health": None,
                "drift": "undeclared",
            }
        )
    for name, state in live.get("docker", {}).items():
        if name in known or _attributed(name, declared_names) is not None:
            continue  # declared, or a sidecar of a declared service
        if _attributed(name, project_names) is None:
            continue  # not tied to a registered project
        out.append(
            {
                "name": name,
                "plane": "infra",
                "runtime": "docker",
                "host": host,
                "machine": host,
                "declared": False,
                "state": state,
                "health": None,
                "drift": "undeclared",
            }
        )
    return out


def services(host: str | None = None, probe: bool = True) -> list[dict]:
    """The declared fleet joined with live state. `probe=False` returns declared only."""
    rows = [dict(s) for s in declared(host)]
    for r in rows:
        r["declared"] = True
        r["state"] = "unknown"
        r["health"] = None
        r["drift"] = None
    if not probe:
        return rows

    lctl = _launchctl()
    ldocker = _local_docker()
    live = _remote(rows)
    for r in rows:
        unit = r.get("unit")
        if not unit:
            continue
        if r.get("runtime") == "launchagent":
            r["state"] = lctl.get(unit, "absent")
        elif r.get("host") == "mac":
            r["state"] = ldocker.get(unit, "absent")
        elif unit in live["systemd"]:
            r["state"] = live["systemd"][unit]
        elif unit in live["docker"]:
            r["state"] = live["docker"][unit]
        elif not live["reachable"]:
            r["state"] = "unknown"
        else:
            r["state"] = "absent"
    _health(rows)
    for r in rows:
        drift = []
        if r.get("state") in ("down", "absent"):
            drift.append(str(r["state"]))
        h = r.get("health")
        if h is not None and not h.get("ok"):
            drift.append("unreachable")
        r["drift"] = ",".join(drift) if drift else None
    if live["reachable"] and host != "mac":
        rows.extend(_undeclared(rows, live, host or _default_host()))
    return rows


def summary(rows: list[dict]) -> dict:
    states: dict[str, int] = {}
    for r in rows:
        key = r.get("state", "unknown")
        states[key] = states.get(key, 0) + 1
    return {
        "total": len(rows),
        "declared": sum(1 for r in rows if r.get("declared", True)),
        "states": states,
        "drifting": sum(1 for r in rows if r.get("drift")),
        "planes": {p: sum(1 for r in rows if r.get("plane") == p) for p in PLANES},
    }


def render(rows: list[dict] | None = None, *, probe: bool = True) -> str:
    rows = services(probe=probe) if rows is None else rows
    s = summary(rows)
    states = "  ".join(f"{k} {v}" for k, v in sorted(s["states"].items()))
    lines = [f"fleet — {s['declared']} declared  ·  {states}"]
    flags = {"up": "ok ", "down": "DWN", "absent": "ABS", "unknown": " ? "}
    for plane in PLANES:
        group = [r for r in rows if r.get("plane") == plane and r.get("declared", True)]
        if not group:
            continue
        lines.append(f"  {plane} ({len(group)})")
        for r in group:
            flag = flags.get(r.get("state", "unknown"), " ? ")
            where = r.get("bind") or r.get("url") or r.get("unit") or "—"
            pad = " " * max(1, 22 - len(str(r.get("name", ""))))
            lines.append(
                f"    [{flag}] {r.get('name', ''):}{pad}{r.get('runtime', ''):11} {where:34} {r.get('purpose', '')}"
            )
    undeclared = [r for r in rows if r.get("declared") is False]
    if undeclared:
        lines.append(f"  undeclared ({len(undeclared)})")
        for r in undeclared:
            pad = " " * max(1, 22 - len(str(r.get("name", ""))))
            lines.append(f"    [ ? ] {r.get('name', '')}{pad}{r.get('runtime', ''):11} {r.get('machine', '')}")
    drifting = [r for r in rows if r.get("drift")]
    if drifting:
        lines.append("  drift: " + ", ".join(f"{r.get('name')} ({r['drift']})" for r in drifting))
    return "\n".join(lines)


def status(probe: bool = True) -> str:
    return render(probe=probe)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    probe = "--offline" not in argv
    rows = services(probe=probe)
    if "--json" in argv:
        print(json.dumps({"summary": summary(rows), "services": rows}, indent=2))
    else:
        print(render(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
