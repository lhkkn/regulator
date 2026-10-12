"""`conductor stack` — a text map of the workspace *stack*.

Prints the three areas and their layers, annotated with counts and the registry
by tier. Structural + offline (fast); run `conductor brief` for live health.

    python3 -m conductor stack
"""

from __future__ import annotations

import json
from pathlib import Path

from conductor.projects import load, registry_path, stack_root, workspace_root


def _kids(p: Path) -> list[str]:
    if not p.is_dir():
        return []
    return sorted(d.name for d in p.iterdir() if d.is_dir() and not d.name.startswith(".") and d.name != "__pycache__")


def _files(p: Path) -> list[str]:
    if not p.is_dir():
        return []
    return sorted(f.name for f in p.iterdir() if f.is_file() and not f.name.startswith("."))


def _count_registry_keys(root: Path) -> int:
    try:
        reg = json.loads((stack_root() / "models" / "keys" / "registry.json").read_text())
        return len(reg.get("keys", []))
    except (OSError, ValueError):
        return 0


def _tiers(root: Path) -> dict[str, list[str]]:
    tiers: dict[str, list[str]] = {}
    try:
        entries = load().get("project", [])
    except Exception:
        return tiers
    for p in entries:
        tiers.setdefault(p.get("tier", "?"), []).append(p["name"])
    return {k: sorted(v) for k, v in sorted(tiers.items())}


def render() -> str:
    root = workspace_root()
    if root is None:
        return "Workspace stack — (no workspace configured; run `regulator init`)"
    out: list[str] = [f"Workspace stack — {root}", ""]

    out.append("▸ Stack/  (the machine)")
    models = _kids(stack_root() / "models")
    out.append(f"    models/    {', '.join(models)}   · keys: {_count_registry_keys(root)} registered")
    harness = _kids(stack_root() / "harness")
    cfgs = _files(stack_root() / "harness" / "configs")
    out.append(f"    harness/   {', '.join(harness) or '—'}" + (f"   · configs: {', '.join(cfgs)}" if cfgs else ""))
    agents = _kids(stack_root() / "agents")
    defs = _files(stack_root() / "agents" / "definitions")
    out.append(f"    agents/    {', '.join(agents) or '—'}" + (f"   · definitions: {', '.join(defs)}" if defs else ""))
    out.append(f"    control/   conductor  · registry: {registry_path()}")

    out.append("")
    out.append("▸ work/  (the outputs)")
    apps = _kids(root / "work" / "apps")
    out.append(f"    apps/      {len(apps)}: {', '.join(apps)}")
    for area in ("research", "notes", "tasks", "film", "archive"):
        kids = _kids(root / "work" / area)
        if kids:
            out.append(f"    {area + '/':<10} {', '.join(kids)}")

    out.append("")
    out.append("▸ ops/  (plumbing)")
    out.append(f"    {', '.join(_kids(stack_root() / 'ops'))}")

    out.append("")
    out.append("projects (by tier)")
    for tier, names in _tiers(root).items():
        out.append(f"    {tier:<9} {len(names)}: {', '.join(names)}")

    out.append("")
    out.append("live: run `python3 -m conductor brief` (health · spend · drift)")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    print(render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
