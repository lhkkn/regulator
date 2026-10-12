"""MCP client inventory — who else's tools does your agent load?

`conductor/mcp/server.py` serves tools this repo composes, so their descriptions are
trusted. That guarantee stops at the process boundary: the same agent config also loads
**third-party** MCP servers, and per MCP's own security guidance, tool descriptions and
annotations "should be considered untrusted, unless obtained from a trusted server."

Two concrete exposure classes, both present on this machine on 2026-10-11:

* **Unpinned package runners.** `uvx duckduckgo-mcp-server` executes whatever the package
  index serves *today*. Its tool descriptions enter the model's context on every request,
  and its code runs with the agent's own privileges. A pin is the whole mitigation.
* **Undeclared servers.** A server nobody wrote down is a server nobody reviews. The
  inventory is the control: every server in every agent config must appear in
  `mcp_clients.toml` with a reason and a pin policy, or the gate fails.

Read-only, offline, never raises. Reads configs by discovery (`harness/configs/*`), so a
new harness is covered the day its config lands — the same reflex as `check_harnesses`.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

# The declaration file: one [[server]] per third-party MCP server an agent loads.
# name · harness · transport · pin · why. Lives next to this module (policy in code's
# neighbourhood, like permissions.py) so the reason is reviewable in the same diff.
DECLARATIONS = Path(__file__).with_name("mcp_clients.toml")

# A package-runner invocation whose package is not version-pinned.
_RUNNERS = ("uvx", "npx", "pnpm", "bunx")
_PIN = re.compile(r"(@\d|==|@latest$)")


def _strip_jsonc(text: str) -> str:
    """Remove // and /* */ comments from JSONC without touching string contents.

    opencode's config is JSONC. A naive strip corrupts any `//` inside a URL or a
    Windows path, so this walks the text and tracks whether it is inside a string.
    """
    out: list[str] = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if text.startswith("//", i):
            i = text.find("\n", i)
            if i == -1:
                break
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i)
            i = n if end == -1 else end + 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _looks_like_server(spec: dict) -> bool:
    """A server entry names how to reach it.

    Not every key under `mcp` is a server: kimi declares client *tuning* there
    (`[mcp.client] tool_call_timeout_ms = …`). Requiring a transport field is what
    keeps this an inventory instead of a false-positive generator.
    """
    return any(k in spec for k in ("command", "url", "type", "serverUrl", "endpoint"))


def _servers_from_json(harness: str, path: Path) -> list[dict]:
    try:
        data = json.loads(_strip_jsonc(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return []
    block = data.get("mcp")
    if not isinstance(block, dict):
        return []
    found = []
    for name, spec in block.items():
        if not isinstance(spec, dict) or not _looks_like_server(spec):
            continue
        command = spec.get("command")
        command = command if isinstance(command, list) else ([command] if command else [])
        found.append(
            {
                "harness": harness,
                "name": name,
                "transport": spec.get("type") or ("remote" if spec.get("url") else "local"),
                "command": [str(c) for c in command],
                "url": str(spec.get("url") or ""),
                "enabled": bool(spec.get("enabled", True)),
            }
        )
    return found


def _servers_from_toml(harness: str, path: Path) -> list[dict]:
    """kimi declares MCP servers under `mcp_servers` / `[mcp]` — both shapes seen in the wild."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return []
    block = data.get("mcp_servers") or data.get("mcp")
    if not isinstance(block, dict):
        return []
    found = []
    for name, spec in block.items():
        if not isinstance(spec, dict) or not _looks_like_server(spec):
            continue
        command = spec.get("command")
        command = command if isinstance(command, list) else ([command] if command else [])
        found.append(
            {
                "harness": harness,
                "name": name,
                "transport": spec.get("type") or ("remote" if spec.get("url") else "local"),
                "command": [str(c) for c in command],
                "url": str(spec.get("url") or ""),
                "enabled": bool(spec.get("enabled", True)),
            }
        )
    return found


def _configs() -> list[tuple[str, Path]]:
    """(harness, path) for every agent config that may declare MCP servers."""
    out: list[tuple[str, Path]] = []
    try:
        from conductor.projects import stack_root

        cfg_dir = stack_root() / "harness" / "configs"
    except Exception:  # noqa: BLE001
        return out
    if not cfg_dir.is_dir():
        return out
    for p in sorted(cfg_dir.iterdir()):
        if not p.is_file() or p.name.startswith("."):
            continue
        harness = p.stem.split("-")[0]
        if p.suffix in (".json", ".jsonc"):
            out.append((harness, p))
        elif p.suffix in (".toml", ".yml", ".yaml"):
            out.append((harness, p))
    return out


def servers() -> list[dict]:
    """Every MCP server declared in every agent config. Never raises."""
    found: list[dict] = []
    for harness, path in _configs():
        if path.suffix in (".json", ".jsonc"):
            found += _servers_from_json(harness, path)
        elif path.suffix == ".toml":
            found += _servers_from_toml(harness, path)
    return found


def declarations() -> dict[str, dict]:
    """The declared allowlist, keyed by `harness/name`. A missing file declares nothing."""
    try:
        data = tomllib.loads(DECLARATIONS.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return {f"{s.get('harness', '')}/{s.get('name', '')}": s for s in data.get("server", []) if isinstance(s, dict)}


def pin_of(spec: dict) -> str | None:
    """The pinned version of a package-runner server, or None when it is unpinned.

    A `@latest` tag is *not* a pin — it is the absence of one wearing a version's clothes.
    """
    command = spec.get("command") or []
    for i, token in enumerate(command):
        base = Path(token).name
        if base in _RUNNERS:
            if i + 1 >= len(command):
                return None
            pkg = command[i + 1]
            if "@latest" in pkg:
                return None
            return pkg if _PIN.search(pkg) else None
        if "==" in token or re.search(r"@\d", token):  # pinned even without an explicit runner
            return token
    return None


def audit() -> dict:
    """`{servers, undeclared, unpinned, findings}` — the inventory plus what is wrong with it."""
    found = servers()
    declared = declarations()
    undeclared, unpinned = [], []
    for spec in found:
        key = f"{spec['harness']}/{spec['name']}"
        decl = declared.get(key)
        if decl is None:
            undeclared.append(spec)
            continue
        wants_pin = bool(decl.get("pin"))
        if wants_pin and not pin_of(spec):
            unpinned.append(spec)
    findings = []
    for spec in undeclared:
        findings.append(
            f"{spec['harness']}/{spec['name']} ({spec['transport']}) is loaded by an agent but "
            "is not declared in conductor/mcp_clients.toml"
        )
    for spec in unpinned:
        findings.append(
            f"{spec['harness']}/{spec['name']} declares a pin but runs unpinned: "
            f"{' '.join(spec['command']) or spec['url']}"
        )
    return {
        "servers": found,
        "declared": declared,
        "undeclared": undeclared,
        "unpinned": unpinned,
        "findings": findings,
    }


def render(result: dict | None = None) -> str:
    """A short text inventory — for `conductor brief` and the console."""
    r = result or audit()
    lines = [f"MCP clients — {len(r['servers'])} server(s) across the agent configs"]
    for s in r["servers"]:
        key = f"{s['harness']}/{s['name']}"
        decl = r["declared"].get(key)
        if decl is None:
            kind = "UNDECLARED"
        elif decl.get("trust") == "first-party":
            kind = "first-party"
        elif decl.get("pin"):
            kind = "pinned"
        else:
            kind = "third-party"
        target = " ".join(s["command"][-1:]) or s["url"][:52] or s["transport"]
        lines.append(f"  [{kind:11}] {key:26} {s['transport']:7} {target}")
    for f in r["findings"]:
        lines.append(f"  ! {f}")
    return "\n".join(lines)
