"""Worktree orchestration — make one-agent-per-tree the default, not a remembered rule.

The hard rule lives in `AGENTS.md` and the recipe in
`../Workspace/work/notes/PARALLEL-AGENTS.md`, but nothing enforced or surfaced it: an
agent had to *remember* to isolate when a second writer was active. This module is the
read half of the fix (discover the live worktrees from git — never hand-maintained) plus
the actuator helpers behind `worktree_add` / `worktree_remove`.

Read (discover, don't hand-maintain):
    worktree.list_for(name)   — a project's worktrees (porcelain parse of `git worktree list`)
    worktree.discover()       — every active project that has a linked worktree
    worktree.render(project)  — the text surface (MCP tool + CLI)
    worktree.detect(projects) — reconcile findings (informational + the real risk)

Write (actuator helpers, via `conductor.contracts`):
    worktree.add(name, task, agent)     — `git worktree add <sibling> -b <agent>/<task>`
    worktree.remove(name, task, force)  — `git worktree remove`, branch survives

The one signal that is a *risk* (not just information): a linked worktree is active while
the shared checkout still has uncommitted work — two writers over the same files, the
failure mode this guard exists to catch. Everything else is reported, not warned.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from conductor.projects import locate
from conductor.projects import projects as registered

OK, WARN, BAD = "  ok  ", " WARN ", " FAIL "

_NAME = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*\Z")


def _git_env() -> dict[str, str]:
    """The environment for a git call, with git's own `GIT_*` vars stripped.

    Inside a git hook (e.g. the pre-commit that runs `conductor check`) git exports
    `GIT_DIR` / `GIT_INDEX_FILE` for the *outer* repo. A child `git -C <other> …` would
    otherwise inherit them and resolve to the wrong repo. `-C` must be the only thing
    that chooses the repo."""
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _git(d: Path, *args: str, timeout: int = 15) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(d), *args], capture_output=True, text=True, timeout=timeout, env=_git_env()
        ).stdout.strip()
    except Exception:
        return ""


def is_repo(d: Path) -> bool:
    """True if `d` is a git working tree (`.git` is a dir in the main checkout, a file
    in a linked worktree)."""
    return (d / ".git").exists()


def is_linked(d: Path) -> bool:
    """True if `d` is a *linked* worktree — a second working copy, not a main checkout.

    The difference is one character of filesystem: a main checkout's `.git` is a
    directory, a linked worktree's is a file holding `gitdir: …`. It matters to
    `registry_drift`, which walks `work/apps` and `work/research` looking for project
    directories: a worktree is a copy of a project that is *already registered*, so
    reporting it as "on disk but not in registry.toml" is a false positive — and one that
    sat in every reconcile run until it was furniture.
    """
    return (d / ".git").is_file()


def _records(d: Path) -> list[dict]:
    """Parse `git worktree list --porcelain` into records (first = the main checkout)."""
    out = _git(d, "worktree", "list", "--porcelain")
    recs: list[dict] = []
    cur: dict | None = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            if cur is not None:
                recs.append(cur)
            cur = {"path": line[len("worktree ") :], "branch": None, "head": None, "detached": False, "bare": False}
        elif cur is None:
            continue
        elif line.startswith("HEAD "):
            cur["head"] = line[len("HEAD ") :].strip()
        elif line.startswith("branch "):
            ref = line[len("branch ") :].strip()
            cur["branch"] = ref[len("refs/heads/") :] if ref.startswith("refs/heads/") else ref
        elif line == "detached":
            cur["detached"] = True
        elif line == "bare":
            cur["bare"] = True
    if cur is not None:
        recs.append(cur)
    return recs


def list_for(name: str) -> list[dict]:
    """All worktrees of a project's repo (main checkout first), or [] if not a repo."""
    d = locate(name, include_archive=False)
    if d is None or not is_repo(d):
        return []
    return _records(d)


def linked(name: str) -> list[dict]:
    """The linked worktrees only (the parallel agents) — excludes the main checkout."""
    recs = list_for(name)
    return recs[1:] if len(recs) > 1 else []


def discover(statuses: tuple[str, ...] = ("active", "maintenance")) -> list[dict]:
    """Every project with at least one linked worktree: {project, main, worktrees}."""
    found: list[dict] = []
    for p in registered():
        if p.get("status") not in statuses:
            continue
        name = str(p.get("name") or "")
        wts = linked(name)
        if wts:
            main = locate(name, include_archive=False)
            found.append({"project": name, "main": str(main) if main else None, "worktrees": wts})
    return found


def _shared_dirty(name: str) -> bool:
    d = locate(name, include_archive=False)
    if d is None or not is_repo(d):
        return False
    return bool(_git(d, "status", "--porcelain"))


def detect(projects: list[dict]) -> list[tuple[str | None, str]]:
    """Reconcile findings (tag, message) for concurrency — data, never prints."""
    findings: list[tuple[str | None, str]] = []
    for p in projects:
        if p.get("status") not in ("active", "maintenance"):
            continue
        name = str(p.get("name") or "")
        wts = linked(name)
        if not wts:
            continue
        branches = ", ".join(str(w.get("branch") or "detached") for w in wts)
        findings.append((OK, f"{name}: {len(wts)} linked worktree(s) — {branches} (agents isolated)"))
        if _shared_dirty(name):
            findings.append(
                (
                    WARN,
                    f"{name}: shared checkout dirty while a worktree is active — two writers "
                    "risk collision (see work/notes/PARALLEL-AGENTS.md)",
                )
            )
    return findings


def render(project: str | None = None) -> str:
    """Text surface: one project's worktrees, or every project that has a linked tree."""
    if project:
        recs = list_for(project)
        if not recs:
            return f"{project}: no git worktrees (not a git repo, or unknown project)"
        lines = [f"{project} — {len(recs) - 1} linked worktree(s), main = {recs[0]['path']}"]
        for w in recs:
            tag = "main" if w is recs[0] else "linked"
            branch = w.get("branch") or ("detached" if w.get("detached") else "?")
            lines.append(f"  [{tag:6}] {w['path']}  ({branch})")
        return "\n".join(lines)

    found = discover()
    if not found:
        return "worktrees: no linked worktrees on any active project (single-writer trees)."
    lines = [
        "worktrees — linked trees (parallel agents), discovered from git:",
        "reflex: one agent per working tree — a second writer gets its own worktree + branch.",
    ]
    for item in found:
        dirty = "shared checkout DIRTY" if _shared_dirty(item["project"]) else "shared checkout clean"
        lines.append(f"  {item['project']}: {len(item['worktrees'])} linked — {dirty}")
        for w in item["worktrees"]:
            branch = w.get("branch") or ("detached" if w.get("detached") else "?")
            lines.append(f"      {w['path']}  ({branch})")
    return "\n".join(lines)


# --- actuator helpers (called from conductor/contracts.py) --------------------


def _valid(*parts: str) -> bool:
    return all(_NAME.match(part) for part in parts)


def target(name: str, task: str) -> Path | None:
    """The sibling directory for a task's worktree: `<checkout>-<task>` next to the
    checkout, so a project's worktrees cluster and never pollute the main tree."""
    d = locate(name, include_archive=False)
    if d is None:
        return None
    return d.parent / f"{d.name}-{task}"


def add(name: str, task: str, agent: str = "agent") -> Path:
    """Create the worktree + branch. Raises on git failure (the contract catches it)."""
    d = locate(name, include_archive=False)
    path = target(name, task)
    if d is None or path is None:
        raise FileNotFoundError(f"no live directory for '{name}'")
    branch = f"{agent}/{task}"
    subprocess.run(
        ["git", "-C", str(d), "worktree", "add", str(path), "-b", branch],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
        env=_git_env(),
    )
    return path


def remove(name: str, task: str, force: bool = False) -> Path:
    """Remove a task's worktree. The branch survives — re-add to restore the files."""
    d = locate(name, include_archive=False)
    path = target(name, task)
    if d is None or path is None:
        raise FileNotFoundError(f"no live directory for '{name}'")
    argv = ["git", "-C", str(d), "worktree", "remove", str(path)]
    if force:
        argv.append("--force")
    subprocess.run(argv, check=True, capture_output=True, text=True, timeout=60, env=_git_env())
    subprocess.run(
        ["git", "-C", str(d), "worktree", "prune"], capture_output=True, text=True, timeout=30, env=_git_env()
    )
    return path


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("list", "ls", "--list"):
        print(render(argv[1] if len(argv) > 1 else None))
        return 0
    verb = argv[0]
    if verb in ("add", "remove", "rm") and len(argv) >= 3:
        from conductor import contracts

        name, task = argv[1], argv[2]
        contract = "worktree.add" if verb == "add" else "worktree.remove"
        args: dict = {"project": name, "task": task, "apply": "--apply" in argv}
        if "--force" in argv:
            args["force"] = True
        if "--agent" in argv:
            args["agent"] = argv[argv.index("--agent") + 1]
        result = contracts.execute(contract, args)
        from conductor.actuator import render as render_result

        print(render_result(result))
        return 0 if result.get("ok") or result.get("outcome") in ("refused", "dry-run") else 1
    print(render(argv[0]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
