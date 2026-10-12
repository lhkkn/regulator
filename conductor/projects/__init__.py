"""Conductor projects module — the project registry, absorbed (Track A1-cont).

The workspace registry (`registry/registry.toml`) is the single source of truth for
project inventory. This module READS it directly — it does not mutate or relocate the
registry. The `registry/` scripts (render.py, reconcile.py, dashboard.py) keep running
unchanged; Conductor is a new front door over the same source. Strangler pattern, not
a big-bang move — see `.context/decisions.md`.

Why not own the data? The registry is a working system with installed LaunchAgents and
its own git history. Conductor wraps it as a consumer first; physical relocation is a
deliberate, separate step (LaunchAgent path updates + reconcile path math + history).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

STATUS_ORDER = ["active", "maintenance", "dormant", "archived", "vendor", "empty"]


def stack_root() -> Path:
    """The machine's home (`~/Stack`): the repo holding `control/`, `models/`,
    `harness/`, `ops/`. Derived from this file's location:
    `<stack>/control/conductor/projects/__init__.py` → parents[3]."""
    return Path(__file__).resolve().parents[3]


def workspace_root() -> Path | None:
    """The workspace path, or None — never raises.

    Order: `CONDUCTOR_WORKSPACE_ROOT` env → `config.toml workspace_root` → `~/Workspace` → walking
    up from this file for a `.workspace-root` sentinel. A candidate counts only if it
    holds the `.workspace-root` marker (or is the env/config value, which wins outright).
    """
    from conductor import config

    candidate = config.setting("workspace_root")
    if candidate:
        return Path(candidate).expanduser().resolve()
    default = Path.home() / "Workspace"
    if (default / ".workspace-root").exists():
        return default
    base = Path(__file__).resolve()
    for parent in [base, *base.parents]:
        if (parent / ".workspace-root").exists():
            return parent
    return None


def find_workspace_root(start: Path | None = None) -> Path:
    """Locate the workspace, or raise — the strict form for callers that require it."""
    root = workspace_root()
    if root is not None:
        return root
    if start is not None:
        base = start.resolve()
        for parent in [base, *base.parents]:
            if (parent / ".workspace-root").exists():
                return parent
    raise FileNotFoundError("could not locate workspace root (.workspace-root). set CONDUCTOR_WORKSPACE_ROOT.")


def registry_path() -> Path:
    """The registry.toml path: `CONDUCTOR_REGISTRY_FILE` env > `registry_file` config > the
    workspace default `<workspace>/registry/registry.toml`."""
    from conductor import config

    env = config.setting("registry_file")
    if env:
        return Path(env).expanduser()
    return find_workspace_root() / "registry" / "registry.toml"


def locate(name: str, *, root: Path | None = None, include_archive: bool = True) -> Path | None:
    """Resolve a project name to its directory, or None — the single resolver.

    `conductor` is the control plane (`Stack/control`); everything else is searched in
    `work/apps`, `work/research`, `[work/archive]`, the workspace root, then a nested
    `work/archive/<group>/<name>`. `include_archive=False` skips archived trees (the
    actuator registers live directories only). `root` overrides the workspace (for
    callers that already hold one).
    """
    if name == "conductor":
        d = stack_root() / "control"
        return d if d.is_dir() else None
    if root is None:
        try:
            root = find_workspace_root()
        except FileNotFoundError:
            return None
    candidates = [root / "work" / "apps" / name, root / "work" / "research" / name]
    if include_archive:
        candidates.append(root / "work" / "archive" / name)
    candidates.append(root / name)
    for d in candidates:
        if d.is_dir():
            return d
    if include_archive:
        for d in (root / "work" / "archive").glob(f"*/{name}"):
            if d.is_dir():
                return d
    return None


def load() -> dict:
    """The parsed registry.toml, or {} when absent — no workspace or not yet `init`ed.

    Degrades instead of raising: an unconfigured install reports an empty inventory, not
    a traceback. The strict `find_workspace_root()` stays the explicit path for callers
    that require a workspace.
    """
    try:
        path = registry_path()
    except FileNotFoundError:
        return {}
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except OSError:
        return {}


def projects() -> list[dict]:
    return load().get("project", [])


def group_by_status() -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for p in projects():
        groups.setdefault(p.get("status", "unknown"), []).append(p)
    return groups
