"""Context-engineering helpers exposed to agents (Track D2).

`build_context`  — assemble curated context for a project (strangler wrapper around the
                  workspace's Stack/harness/context/context.py; not yet ported natively).
`project_state`  — a project's living state (.context/state.md, or a freshly rendered
                  generated block if absent).

Both are read-only: they never modify the target project.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from conductor.projects import locate, stack_root


def _project_dir(project: str) -> Path | None:
    return locate(project)


def build_context(
    project: str, files: list[str] | None = None, exclude: list[str] | None = None, since: str | None = None
) -> str:
    """Assemble curated context for a project via `Stack/harness/context/context.py build`.

    Strangler wrapper (same pattern as the registry port): subprocess until context/ is
    absorbed natively. Read-only. `since` scopes the output to a git diff (ref / 'auto'
    / N) — embeds changed files + a changes summary, for refactor-audit workflows.
    """
    pdir = _project_dir(project)
    if pdir is None:
        return f"project not found: {project} (expected under projects/{project})"
    script = stack_root() / "harness" / "context" / "context.py"
    if not script.is_file():
        return f"context tool not found: {script}"
    cmd = [sys.executable, str(script), "build", str(pdir)]
    for pat in files or []:
        cmd += ["--files", pat]
    for pat in exclude or []:
        cmd += ["--exclude", pat]
    if since:
        cmd += ["--since", since]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    out = proc.stdout
    if proc.returncode != 0 and proc.stderr.strip():
        out += ("\n" if out else "") + proc.stderr.strip()
    # Fold in durable memory so the context carries what the stack has learned.
    from conductor import memory

    try:
        mem = memory.recent(12)
        if mem:
            out += "\n\n## Memory (durable facts)\n" + memory.render(mem) + "\n"
    except Exception:  # noqa: BLE001 — memory is best-effort
        pass
    # Surface the parallel-agent reflex so isolation is the default, not a remembered rule.
    try:
        from conductor import worktree

        wts = worktree.linked(project)
        note = ["", "## Parallel agents", "- One agent per working tree (work/notes/PARALLEL-AGENTS.md)."]
        if wts:
            note.append(f"- {len(wts)} linked worktree(s) active — another agent may be working here:")
            for w in wts:
                branch = w.get("branch") or ("detached" if w.get("detached") else "?")
                note.append(f"  - {w['path']} ({branch})")
            note.append(
                "- Work in your own tree (`conductor worktree add "
                + project
                + " <task>`); do not write the shared checkout."
            )
        out += "\n".join(note) + "\n"
    except Exception:  # noqa: BLE001 — best-effort, like memory
        pass
    return out


def project_state(project: str) -> str:
    """Return a project's living state: its `.context/state.md` if present, else a
    freshly rendered generated block. Non-mutating — never writes to the project."""
    from conductor.context.state import render_block

    pdir = _project_dir(project)
    if pdir is None:
        return f"project not found: {project} (expected under projects/{project})"
    state_file = pdir / ".context" / "state.md"
    if state_file.is_file():
        return state_file.read_text()
    return "# State\n\n" + render_block(pdir)
