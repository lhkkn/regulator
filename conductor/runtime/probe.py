"""System + agent-stack probes. Every probe is defensive: a missing tool or failed
command yields None/[] in that field — never an exception (the UI renders SOURCE
OFFLINE tiles, and one dead probe must not kill the rest).

Stdlib-only by house rule; where macOS has no syscall binding we parse the canonical
command's output (vm_stat, pmset, netstat, ps, launchctl) with short timeouts.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import time
import tomllib
import urllib.request
from pathlib import Path

from conductor import config

HOME = Path.home()
TIMEOUT = 4


def _run(cmd: list[str], timeout: int = TIMEOUT) -> str | None:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.stdout if p.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _http_json(url: str, timeout: float = 1.0) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- machine stats


def _memory() -> dict | None:
    out = _run(["vm_stat"])
    total_s = _run(["sysctl", "-n", "hw.memsize"])
    if not out or not total_s:
        return None
    m = re.search(r"page size of (\d+)", out)
    ps = int(m.group(1)) if m else 16384

    def pages(name: str) -> int:
        m = re.search(rf"^{re.escape(name)}:\s+(\d+)\.", out, re.M)
        return int(m.group(1)) if m else 0

    total = int(total_s.strip())
    # Activity-Monitor-style "used": active + wired + compressor-occupied (approximation).
    used = (pages("Pages active") + pages("Pages wired down") + pages("Pages occupied by compressor")) * ps
    return {
        "total_gb": round(total / 2**30, 1),
        "used_gb": round(used / 2**30, 1),
        "percent": round(100 * used / total, 1) if total else None,
    }


def _battery() -> dict | None:
    out = _run(["pmset", "-g", "batt"])
    if not out or "InternalBattery" not in out:
        return None
    m = re.search(r"(\d+)%;", out)
    return {
        "percent": int(m.group(1)) if m else None,
        "charging": "AC Power" in out,
        "state": "charged" if "charged" in out else ("charging" if " charging;" in out else "discharging"),
    }


def _uptime() -> dict | None:
    out = _run(["sysctl", "-n", "kern.boottime"])
    if not out:
        return None
    m = re.search(r"sec = (\d+)", out)
    if not m:
        return None
    secs = int(time.time()) - int(m.group(1))
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    return {"seconds": secs, "human": f"{days}d {hours}h {rem // 60}m"}


_NET_STATE: dict = {}


def _net() -> dict | None:
    """Throughput across all interfaces, from netstat -ibn deltas between calls."""
    out = _run(["netstat", "-ibn"])
    if not out:
        return None
    ibytes = obytes = 0
    for line in out.splitlines()[1:]:
        cols = line.split()
        if len(cols) >= 10 and cols[0] != "Name" and "." in cols[3]:  # has an IP address
            try:
                ibytes += int(cols[6])
                obytes += int(cols[9])
            except (ValueError, IndexError):
                continue
    now = time.time()
    prev = _NET_STATE.get("prev")
    _NET_STATE["prev"] = (now, ibytes, obytes)
    if not prev:
        return {"down_mbps": None, "up_mbps": None}  # first sample: rate needs a delta
    dt = now - prev[0]
    if dt <= 0:
        return {"down_mbps": None, "up_mbps": None}
    return {
        "down_mbps": round((ibytes - prev[1]) / dt / 2**20, 2),
        "up_mbps": round((obytes - prev[2]) / dt / 2**20, 2),
    }


def _top(n: int = 6) -> list[dict]:
    out = _run(["ps", "-Ao", "pid,pcpu,pmem,comm", "-r"])
    if not out:
        return []
    rows = []
    for line in out.splitlines()[1 : n + 1]:
        cols = line.split(None, 3)
        if len(cols) == 4:
            rows.append({"pid": cols[0], "cpu": float(cols[1]), "mem": float(cols[2]), "command": Path(cols[3]).name})
    return rows


# Known local services: (port, label, http-link?) — read, not hand-maintained state:
# liveness is probed; the list itself is the attribution map for what the machine runs.
# The default set is standard third-party tools only; the operator's own services are
# declared in config.toml: known_ports = [{port = 8100, label = "my app", http = true}]
KNOWN_PORTS = [
    (11434, "ollama", False),
    (1234, "lm studio", False),
    (18789, "openclaw gateway", False),
]


def _known_ports() -> list[tuple[int, str, bool]]:
    extra = config.value("known_ports", [])
    rows = list(KNOWN_PORTS)
    for e in extra:
        try:
            rows.append((int(e["port"]), str(e["label"]), bool(e.get("http", False))))
        except (KeyError, TypeError, ValueError):
            continue
    return rows


def _ports() -> list[dict]:
    rows = []
    for port, label, http in _known_ports():
        live = False
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                live = True
        except OSError:
            pass
        rows.append(
            {"port": port, "label": label, "live": live, "url": f"http://127.0.0.1:{port}" if http and live else None}
        )
    return rows


def _docker() -> list[dict] | None:
    if not shutil.which("docker"):
        return None
    out = _run(["docker", "ps", "--format", "{{json .}}"], timeout=6)
    if out is None:
        return None
    rows = []
    for line in out.splitlines():
        try:
            c = json.loads(line)
            rows.append({"name": c.get("Names"), "status": c.get("Status"), "ports": c.get("Ports")})
        except json.JSONDecodeError:
            continue
    return rows


def system() -> dict:
    load = os.getloadavg()
    return {
        "cpu": {
            "load1": round(load[0], 2),
            "load5": round(load[1], 2),
            "load15": round(load[2], 2),
            "cores": os.cpu_count(),
        },
        "memory": _memory(),
        "disk": (
            lambda u: {
                "total_gb": round(u.total / 2**30, 1),
                "used_gb": round(u.used / 2**30, 1),
                "free_gb": round(u.free / 2**30, 1),
                "percent": round(100 * u.used / u.total, 1),
            }
        )(shutil.disk_usage("/")),
        "uptime": _uptime(),
        "battery": _battery(),
        "net": _net(),
        "top": _top(),
        "ports": _ports(),
        "docker": _docker(),
    }


# ---------------------------------------------------------------- agent stack


def _jsonc(path: Path) -> dict | None:
    """Parse JSON-with-comments (opencode config) leniently. Comment stripping is
    string-aware: `//` inside a quoted value (e.g. a https:// URL) is NOT a comment."""
    try:
        text = path.read_text()
    except OSError:
        return None
    out_lines = []
    for line in text.splitlines():
        in_str = esc = False
        cut = len(line)
        for i, ch in enumerate(line):
            if esc:
                esc = False
            elif ch == "\\" and in_str:
                esc = True
            elif ch == '"':
                in_str = not in_str
            elif ch == "/" and not in_str and i + 1 < len(line) and line[i + 1] == "/":
                cut = i
                break
        out_lines.append(line[:cut])
    text = re.sub(r",(\s*[}\]])", r"\1", "\n".join(out_lines))
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _toml(path: Path) -> dict | None:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return None


def _version(cmd: list[str]) -> str | None:
    out = _run(cmd, timeout=6)
    return out.strip().splitlines()[0] if out and out.strip() else None


def _du_mb(path: Path) -> float | None:
    try:
        total = sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
        return round(total / 2**20, 1)
    except OSError:
        return None


def _opencode() -> dict:
    binary = HOME / ".opencode" / "bin" / "opencode"
    cfg = _jsonc(HOME / ".config" / "opencode" / "opencode.jsonc") or {}
    mcp = cfg.get("mcp") or {}
    return {
        "installed": binary.exists(),
        "version": _version([str(binary), "--version"]) if binary.exists() else None,
        "config": str(HOME / ".config" / "opencode" / "opencode.jsonc"),
        "mcp_servers": sorted(mcp.keys()),
        "model": cfg.get("model"),
    }


def _kimi() -> dict:
    cfg_path = HOME / ".kimi-code" / "config.toml"
    cfg = _toml(cfg_path) or {}
    skills = (
        sorted(p.parent.name for p in (HOME / ".kimi-code" / "skills").glob("*/SKILL.md"))
        if (HOME / ".kimi-code" / "skills").is_dir()
        else []
    )
    raw_hooks = cfg.get("hooks") or []
    hooks = (
        [h.get("event", "?") for h in raw_hooks if isinstance(h, dict)]
        if isinstance(raw_hooks, list)
        else list(raw_hooks.keys())
    )
    models = cfg.get("models") or {}
    return {
        "installed": cfg_path.exists(),
        "version": _version(["kimi", "--version"]),
        "config": str(cfg_path),
        "default_model": cfg.get("default_model"),
        "models": list(models.keys()) if isinstance(models, dict) else [],
        "hooks": hooks,
        "skills": skills,
        "sessions_mb": _du_mb(HOME / ".kimi-code" / "sessions"),
    }


def _claude() -> dict:
    settings = _jsonc(HOME / ".claude" / "settings.json") or {}
    plans = list((HOME / ".claude" / "plans").glob("*.md")) if (HOME / ".claude" / "plans").is_dir() else []
    # claude encodes the project cwd as a dir: slashes become dashes ("/a/b" → "-a-b")
    memory_dir = HOME / ".claude" / "projects" / ("-" + str(HOME).strip("/").replace("/", "-")) / "memory"
    return {
        "installed": (HOME / ".claude").is_dir(),
        "version": _version(["claude", "--version"]),
        "model": settings.get("model"),
        "plans": len(plans),
        "memory_files": [p.name for p in memory_dir.glob("*.md")] if memory_dir.is_dir() else [],
        "config": str(HOME / ".claude" / "settings.json"),
    }


def _ollama() -> dict:
    tags = _http_json("http://127.0.0.1:11434/api/tags")
    return {"running": tags is not None, "models": sorted(m.get("name", "?") for m in (tags or {}).get("models", []))}


def _lmstudio() -> dict:
    live = False
    try:
        with socket.create_connection(("127.0.0.1", 1234), timeout=0.25):
            live = True
    except OSError:
        pass
    models_dir = HOME / ".lmstudio" / "models"
    return {"running": live, "models": sum(1 for p in models_dir.rglob("*.gguf")) if models_dir.is_dir() else 0}


def _openclaw() -> dict:
    plist = HOME / "Library" / "LaunchAgents" / "ai.openclaw.gateway.plist"
    out = _run(["launchctl", "list"]) or ""
    line = next((ln for ln in out.splitlines() if "openclaw" in ln), None)
    state = "not loaded"
    if line:
        cols = line.split("\t")
        state = "running" if cols[0] not in ("-", "0") else f"loaded (last exit {cols[1]})"
    return {"installed": plist.exists(), "state": state, "config": str(plist) if plist.exists() else None}


def _dsh_mcp_servers(home: Path) -> list[str]:
    """MCP `serverName`s declared across every DSH profile patch.

    The patch is YAML and the stdlib has no YAML parser (house rule), so this scans for
    the one key that names a server rather than parsing the document. Deliberately crude,
    and deliberately non-fatal: a miss yields a shorter list, never an exception.
    """
    names: set[str] = set()
    for patch in sorted((home / "profiles").glob("*/cordis.patch.yml")):
        try:
            for line in patch.read_text(encoding="utf-8").splitlines():
                m = re.match(r"\s*serverName:\s*(\S+)", line)
                if m:
                    names.add(m.group(1))
        except OSError:
            continue
    return sorted(names)


def _dsh() -> dict:
    """DeepSeek Harness — profiles, MCP wiring, the user-global instruction file."""
    home = HOME / ".dsh"
    if not home.is_dir():
        return {"installed": False}
    profiles_dir = home / "profiles"
    return {
        "installed": True,
        "version": _version(["dsh", "--version"]),
        "home": str(home),
        # Discovered, not listed: a new profile appears here with no code change.
        "profiles": sorted(p.name for p in profiles_dir.iterdir() if p.is_dir()) if profiles_dir.is_dir() else [],
        "mcp_servers": _dsh_mcp_servers(home),
        "instructions": str(home / "AGENTS.md") if (home / "AGENTS.md").exists() else None,
        "skills": sorted(p.parent.name for p in (home / "skills").glob("*/SKILL.md"))
        if (home / "skills").is_dir()
        else [],
        "sessions_mb": _du_mb(home / "sessions"),
    }


def agents() -> dict:
    """The agentic layer of this machine: CLIs, models, MCP wiring, personas, memory.

    IMPORTANT: every harness listed here must have a probe, and `check_harnesses` fails
    the gate when `harness/configs/` contains a config for a harness this dict omits. A
    hardcoded list silently drifts from reality otherwise — which is how `dsh` came to be
    versioned in `harness/configs/` and visible in `conductor stack` while staying absent
    from `conductor brief`.
    """
    from conductor.mcp.tools import TOOLS

    return {
        "opencode": _opencode(),
        "dsh": _dsh(),
        "kimi": _kimi(),
        "claude": _claude(),
        "ollama": _ollama(),
        "lmstudio": _lmstudio(),
        "openclaw": _openclaw(),
        "mcp_tools": [{"name": t["name"], "description": t["description"]} for t in TOOLS],
    }
