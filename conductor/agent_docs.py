"""Agent-instruction convention guard — `AGENTS.md` canonical, `CLAUDE.md` a symlink.

A real (non-symlink) `CLAUDE.md` is drift: two agent-instruction files that can silently
diverge, so opencode (which reads `AGENTS.md`) and Claude (which reads `CLAUDE.md`) act
on different rules. The convention — `AGENTS.md` canonical, `CLAUDE.md` a symlink to it
(AGENTIC-PRACTICES §2, "one agent-instruction file name") — makes them read one source.

For every *live* registered project with a local tree, every `CLAUDE.md` must be a
symlink whose target is `AGENTS.md` (present, not dangling). Read-only and defensive: it
never writes, never raises, and skips remote/archive-only projects.

    python3 -m conductor.agent_docs
"""

from __future__ import annotations

from pathlib import Path

from conductor import docs_health
from conductor.projects import find_workspace_root, load


def _claude_files(root: Path) -> list[Path]:
    """Every CLAUDE.md under `root`, excluding vendored/tooling dirs."""
    out = []
    for path in root.rglob("CLAUDE.md"):
        rel = path.relative_to(root)
        if any(part in docs_health.EXCLUDE_DIRS for part in rel.parts):
            continue
        out.append(path)
    return out


def scan_project(project: dict, workspace: Path) -> list[str]:
    """Convention failures for one project's CLAUDE.md files. Never raises."""
    if project.get("status") == "archived":
        return []  # retired trees are cold — the rule guards live ones
    root = docs_health._project_root(workspace, project)
    if root is None:
        return []  # remote/absent — nothing on disk to guard
    name = project.get("name", "?")
    failures = []
    for p in _claude_files(root):
        rel = p.relative_to(root).as_posix()
        if not p.is_symlink():
            failures.append(f"{name}: {rel} is a real file — drift risk (two agent-instruction files that can diverge)")
            continue
        target = p.readlink()
        if target.name != "AGENTS.md" or not (p.parent / "AGENTS.md").is_file():
            failures.append(f"{name}: {rel} -> {target} (must be a symlink to AGENTS.md)")
    return failures


def report() -> list[str]:
    """Convention failures across the workspace. Defensive — never raises."""
    try:
        workspace = find_workspace_root()
        failures = []
        for project in load().get("project", []):
            failures.extend(scan_project(project, workspace))
        return failures
    except Exception as exc:  # noqa: BLE001
        return [f"(workspace) agent-docs guard failed: {exc}"]


def main() -> int:
    failures = report()
    if not failures:
        print("agent-docs: OK — every CLAUDE.md is a symlink to AGENTS.md")
        return 0
    print(f"agent-docs: {len(failures)} issue(s):")
    for f in failures:
        print(f"  - {f}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
