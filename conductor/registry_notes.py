"""Registry notes lint — the one registry field with no gate.

`registry.toml` enforces "discover, don't hand-maintain" for every structured field:
`deploy` is probed, `prod_branch` is read from git, `url` is fetched, `port` is checked
against systemd. `notes` is free prose that nothing reads — so it is the field that
rots, and it carries the most load-bearing claims in the file.

The failure mode is not a stale number. It is that an **agent reads `notes` as
authority** and plans around a world that no longer exists: building a training run
that already shipped, or declining to commit to a repo it believes is not one.

Two rule tiers, both deliberately narrow (high precision, low recall — a prose regex
cannot tell a claim from a quotation, so the rule text is the real deliverable):

* **fail** — negative present-tense assertions ("no X yet", "not a Y"). These describe
  a state that flips *silently*: the day the thing becomes true, the sentence is a lie
  and nothing notices. Write the positive fact, or point at where it is recorded.
* **warn** — notes long enough to be a document, and counts of things that change. A
  number a probe could give you does not belong in a file no probe reads.

Read-only, offline, never raises. Mirrors `docs_health` (report → flags → gate) so the
two guardrails read the same way.
"""

from __future__ import annotations

import re

# ── rules ────────────────────────────────────────────────────────────────────
# (pattern, label, fix). Order is the report order. Keep these HIGH PRECISION: a
# false positive on the fail tier blocks a commit, which is how gates get bypassed.

FAIL_RULES: list[tuple[str, str, str]] = [
    (
        r"\bno\s+[\w-]+(?:\s+[\w-]+){0,2}\s+yet\b",
        'negative present tense ("no … yet")',
        "state the positive fact, or record it where it is generated (runs.toml, git, a probe)",
    ),
    (
        r"\bnot\s+(?:yet|a\s+git\s+repo|a\s+repo|deployed|built|live|tracked|registered)\b",
        'negative present tense ("not a …")',
        "state what it IS; whether it is a git repo is discoverable, so do not claim it here",
    ),
    (
        r"\b(?:has|have)n'?t\b|\bdoes(?:n'?t|\s+not)\s+exist\b|\bnothing\s+(?:is\s+)?(?:built|deployed|yet)\b",
        "asserted absence",
        "asserted absences rot silently — drop it, or add a check that keeps it honest",
    ),
    (
        r"\bas\s+of\s+\d{4}-\d{2}",
        "dated snapshot in prose",
        "dates belong next to the thing they date, not in a field no one re-reads",
    ),
    (
        r"\bcurrently\b|\bright\s+now\b",
        'volatile qualifier ("currently")',
        "the registry describes the durable shape; time-bound state goes in the live views",
    ),
]

WARN_RULES: list[tuple[str, str, str]] = [
    (
        r"\b\d[\d,]*\s+(?:records|expenses|docs|files|tests|commits|lines|tokens|chunks|"
        r"contracts|memberships|users|databases|DBs|services|endpoints)\b",
        "count of a thing that changes",
        "move the number to the project's own docs (or add a check that verifies it)",
    ),
]

NOTES_MAX = 800

_ONE_LINE = re.compile(r"\s+")


def _excerpt(text: str, match: re.Match[str], width: int = 60) -> str:
    """The matched text with a little context — enough to find it in the file."""
    start = max(0, match.start() - 24)
    end = min(len(text), match.end() + width)
    return _ONE_LINE.sub(" ", ("…" if start else "") + text[start:end] + ("…" if end < len(text) else ""))


def lint_project(project: dict) -> dict:
    """Findings for one project. Never raises. `{name, status, fail: [...], warn: [...]}`."""
    name = project.get("name", "?")
    status = project.get("status", "unknown")
    notes = project.get("notes") or ""
    out: dict = {"name": name, "status": status, "fail": [], "warn": []}
    if status in ("archived", "vendor") or not notes:
        return out  # retired work is out of scope — the sprawl rule applies to live trees
    for pattern, label, fix in FAIL_RULES:
        for m in re.finditer(pattern, notes, re.I):
            out["fail"].append({"rule": label, "excerpt": _excerpt(notes, m), "fix": fix})
    for pattern, label, fix in WARN_RULES:
        for m in re.finditer(pattern, notes, re.I):
            out["warn"].append({"rule": label, "excerpt": _excerpt(notes, m), "fix": fix})
    if len(notes) > NOTES_MAX:
        out["warn"].append(
            {
                "rule": f"notes is {len(notes)} chars (cap {NOTES_MAX})",
                "excerpt": _ONE_LINE.sub(" ", notes[:70]) + "…",
                "fix": "keep the non-derivable why; move the rest into the project's docs/",
            }
        )
    return out


def report(projects: list[dict] | None = None) -> list[dict]:
    """Lint every project in the registry. Defensive: a bad registry yields no rows."""
    if projects is None:
        try:
            from conductor.projects import projects as load_projects

            projects = load_projects()
        except Exception:  # noqa: BLE001 — a lint is never allowed to break the gate
            return []
    return [lint_project(p) for p in projects if isinstance(p, dict)]


def render(rows: list[dict]) -> tuple[str, list[str]]:
    """`(text, fail_flags)` — the same contract as `docs_health.render`."""
    failing = [r for r in rows if r["fail"]]
    warning = [r for r in rows if r["warn"] and not r["fail"]]
    flags: list[str] = []
    for r in failing:
        detail = "; ".join(f"{f['rule']}: {f['excerpt']}" for f in r["fail"][:3])
        flags.append(f"[registry] {r['name']}: notes asserts a state that rots — {detail}")
    lines = []
    if failing:
        lines.append(f"notes lint: {len(failing)} project(s) assert a state that rots")
    for r in warning:
        for w in r["warn"][:2]:
            lines.append(f"  · {r['name']}: {w['rule']} — {w['excerpt']}")
    if not lines:
        lines.append("notes lint: clean")
    return "\n".join(lines), flags
