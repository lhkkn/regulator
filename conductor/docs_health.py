"""Workspace docs-health check — the sprawl guardrail for markdown docs.

For every registered project that exists locally, scan its markdown docs and flag the
failures that make a docs tree unhealthy, while reporting counts so a human can eyeball
growth over time:

  1. **No index** — a project tree has no entry point (`docs/README.md`, `README.md`,
     `CONTEXT.md`, or `AGENTS.md` at the project root). "Every live doc must be
     reachable from an index" — a tree with no front door is undiscoverable.
  2. **Orphan live docs** — `.md` files under `docs/` that are not linked from that
     project's docs index (the "index must not lie" rule, applied workspace-wide).
     Name matching is case-insensitive and tolerant of path prefixes.
  3. **Archive-in-live-tree** — a `docs/archive/` directory inside an *active* project
     whose archive is not a documented, intentional design (the operator lists such
     projects in config.toml as `intentional_live_archive`). Otherwise
     archived material belongs in `projects/archive/`, not the live tree.

Read-only and defensive: it never writes, never raises, and degrades to `ok=False` with
a message if the workspace can't be located. The `check` gate treats failures as spec;
counts are informational (reported, not failed).

    python -m conductor docs-health         # standalone report (exit 1 on failures)
"""

from __future__ import annotations

from pathlib import Path

from conductor import config
from conductor.projects import find_workspace_root, load, locate

# A project's docs tree is "indexed" if any of these exists at the project root.
INDEX_FILES = ("docs/README.md", "docs/index.md", "README.md", "CONTEXT.md", "AGENTS.md")
# dirs never counted as project docs
EXCLUDE_DIRS = {
    "node_modules",
    ".venv",
    "venv",
    "site-packages",
    ".git",
    "__pycache__",
    ".pytest_cache",
    "dist",
    "build",
    ".claude",
}
# archive dir names inside a live tree (the "park your history" rule)
ARCHIVE_DIRS = ("docs/archive", "docs/retired", "docs/retired-specs", "archive")


def _intentional_live_archive() -> set[str]:
    """Projects that intentionally keep an archive inside the live tree (documented
    design). Workspace-specific — declared in config.toml `intentional_live_archive`."""
    return {x.strip() for x in config.setting("intentional_live_archive").split(",") if x.strip()}


def _project_root(workspace: Path, project: dict) -> Path | None:
    """Resolve a registered project to its on-disk root, or None if remote/absent.

    Layout reality: code projects live in projects/<name>, archived ones in
    projects/archive/<name>, tool-projects in tools/<name>, and some (status, github
    pages) are remote-only with no local tree.
    """
    return locate(project.get("name", "?"), root=workspace)


def _find_md(root: Path) -> list[Path]:
    """All markdown files under `root`, excluding vendored/tooling dirs."""
    out = []
    for path in root.rglob("*.md"):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue
        out.append(path)
    return out


def _index_text(root: Path) -> str:
    """Concatenated, lowercased text of the project's index files (the 'front door').

    Includes the root index files plus every nested section `README.md` under docs/ —
    a section's own README is that section's index, so docs linked from it are not
    orphans.
    """
    parts = []
    for rel in INDEX_FILES:
        p = root / rel
        if p.is_file():
            try:
                parts.append(p.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                pass
    docs_dir = root / "docs"
    if docs_dir.is_dir():
        for p in docs_dir.rglob("README.md"):
            if not p.is_file():
                continue
            try:
                parts.append(p.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                pass
    return "\n".join(parts).lower()


def _linked(index_text: str, doc: Path, root: Path) -> bool:
    """True if the doc is referenced from the index (case-insensitive, tolerant).

    Matches the bare filename, the docs/-relative path, and any line that contains the
    filename — so `Overview.md` in the index links `docs/OVERVIEW.md` on disk.
    """
    rel = doc.relative_to(root).as_posix().lower()
    name = doc.name.lower()
    return name in index_text or rel in index_text


def scan_project(project: dict, workspace: Path) -> dict:
    """Health of one project's docs tree. Never raises."""
    name = project.get("name", "?")
    status = project.get("status", "unknown")
    root = _project_root(workspace, project)
    if root is None:
        return {
            "name": name,
            "status": status,
            "ok": True,
            "count": 0,
            "failures": [],
            "skipped": "no local project dir (remote/archive-only)",
        }

    docs = _find_md(root)
    index = _index_text(root)
    has_index = any((root / rel).is_file() for rel in INDEX_FILES)
    intentional_archive = name in _intentional_live_archive()
    orphans = []
    for d in docs:
        if not d.is_relative_to(root / "docs"):
            continue
        # never flag the index files themselves, nor section-level READMEs
        # (a subdir README.md is that section's own index, not an orphan)
        rel = d.relative_to(root).as_posix()
        if rel in INDEX_FILES or d.name.lower() == "readme.md":
            continue
        # don't flag docs under an intentional archive dir (parked by design)
        if intentional_archive and any(part in ARCHIVE_DIRS for part in d.relative_to(root).parts):
            continue
        if not _linked(index, d, root):
            orphans.append(d)
    in_live_tree = status in ("active", "maintenance", "dormant")
    archives = [
        d
        for d in docs
        if in_live_tree and not intentional_archive and any(part in ARCHIVE_DIRS for part in d.relative_to(root).parts)
    ]

    failures = []
    if not has_index and docs:
        failures.append(f"no docs index (expected one of: {', '.join(INDEX_FILES)})")
    if orphans:
        failures.append(
            f"{len(orphans)} orphan doc(s) not linked from index: "
            + ", ".join(str(o.relative_to(root)) for o in orphans[:5])
            + (" ..." if len(orphans) > 5 else "")
        )
    if archives:
        failures.append(
            f"{len(archives)} archived doc(s) inside live tree (use "
            "projects/archive/): " + ", ".join(str(a.relative_to(root)) for a in archives[:5])
        )

    # Archived projects report counts + issues but never block the gate — the
    # sprawl rule applies to live trees, not retired ones.
    if status == "archived":
        return {
            "name": name,
            "status": status,
            "ok": True,
            "count": len(docs),
            "failures": [f"(archived — {f})" for f in failures],
        }

    return {"name": name, "status": status, "ok": not failures, "count": len(docs), "failures": failures}


def report() -> list[dict]:
    """Health of every registered project's docs tree. Defensive — never raises."""
    try:
        workspace = find_workspace_root()
        rows = [scan_project(p, workspace) for p in load().get("project", [])]
    except Exception as exc:
        return [{"name": "(workspace)", "status": "error", "ok": False, "count": 0, "failures": [str(exc)]}]
    return rows


def render(rows: list[dict]) -> tuple[list[str], list[str]]:
    """Pretty-print rows (body only — no section header; the caller owns it).
    Returns (lines, flags) — flags are the check-gate failures.
    """
    lines, flags = [], []
    total = sum(r["count"] for r in rows)
    lines.append(f"- {total} markdown docs across {len(rows)} registered projects")
    for row in sorted(rows, key=lambda r: (-r["count"], r["name"])):
        mark = "ok" if row["ok"] else "FAIL"
        lines.append(f"- [{mark}] {row['name']} ({row['status']}): {row['count']} docs")
        for f in row["failures"]:
            lines.append(f"    - {f}")
            if not row["ok"]:  # archived rows report issues but don't block the gate
                flags.append(f"[docs] {row['name']}: {f}")
    return lines, flags


def main() -> int:
    rows = report()
    lines, flags = render(rows)
    print("## Docs health")
    print("\n".join(lines))
    return 1 if flags else 0


if __name__ == "__main__":
    raise SystemExit(main())
