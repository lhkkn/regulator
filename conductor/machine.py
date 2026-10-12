"""`conductor machine` — a machine-level view (the scope above the workspace).

The stack is machine-scoped: it can reason about the whole computer, not just
`~/Workspace`. This discovers what's out there — home layout, git repos anywhere,
local services, containers, backups — read-only and bounded.

    python3 -m conductor machine
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from conductor import config, proc
from conductor.projects import workspace_root

_SKIP_DIRS = {
    "Library",
    ".Trash",
    ".cache",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".npm",
    ".gem",
    ".cargo",
    ".rustup",
    ".gradle",
    ".m2",
    "Applications",
    "Movies",
    "Music",
    "Pictures",
    "Public",
    "Creative Cloud Files",
}
_MAXDEPTH = 4
_CAP = 60


def _git_repos(home: Path) -> list[str]:
    """Git repos under home, bounded (depth ≤ _MAXDEPTH, heavy dirs pruned)."""
    repos: list[str] = []
    for dirpath, dirnames, _ in os.walk(home):
        rel = Path(dirpath).relative_to(home)
        if len(rel.parts) > _MAXDEPTH:
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        if ".git" in dirnames:
            repos.append("~/" + str(rel) if str(rel) != "." else "~")
            dirnames.remove(".git")
        if len(repos) >= _CAP:
            break
    return sorted(set(repos))


def _launch_agents() -> list[str]:
    """The user's own agents (~/Library/LaunchAgents), not the 500 system ones."""
    d = config.launch_agents_dir()
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.plist"))


def _containers() -> list[str]:
    if not proc.find("docker", "/usr/local/bin/docker", "/opt/homebrew/bin/docker"):
        return []
    try:
        out = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"], capture_output=True, text=True, timeout=10
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [x for x in out.splitlines() if x.strip()]


def _backup_age() -> str:
    try:
        from conductor.backup import latest

        rec = latest()
        return f"{rec['age_hours']}h ago" if rec.get("ok") else (rec.get("error") or "unknown")
    except Exception as exc:  # noqa: BLE001
        return f"unavailable ({exc})"


def _keys_count() -> int:
    try:
        from conductor.keys.api import inventory

        return len(inventory())
    except Exception:  # noqa: BLE001
        return 0


def render() -> str:
    home = Path.home()
    ws = workspace_root()
    ws_shown = f"{ws}   (set CONDUCTOR_WORKSPACE_ROOT to target another)" if ws else "(not set — run `regulator init`)"

    home_dirs = sorted(d.name for d in home.iterdir() if d.is_dir() and not d.name.startswith("."))
    repos = _git_repos(home)
    agents = _launch_agents()
    containers = _containers()

    out = [
        f"Machine — {os.uname().nodename}",
        f"  home       {home}",
        f"  workspace  {ws_shown}",
        "",
        f"  home dirs  ({len(home_dirs)}): {', '.join(home_dirs)}",
        f"  git repos  ({len(repos)}): {', '.join(repos) if repos else '—'}",
        f"  agents     ({len(agents)}): {', '.join(agents) if agents else '—'}",
    ]
    if containers:
        out.append(f"  containers ({len(containers)}): {', '.join(containers)}")
    out += [
        f"  backups    restic→B2: {_backup_age()}",
        f"  keys       {_keys_count()} registered",
    ]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    print(render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
