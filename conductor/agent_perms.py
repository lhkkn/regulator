"""Agent permission lint — an agent that can run `conductor` must not answer CRPs.

The CRP's whole mechanism is the asymmetry: **agents file, humans answer.** An agent whose
bash allowlist contains `python3 -m conductor *` inherits `conductor crp answer` along with
everything else — so the asymmetry is exactly one broad allowlist entry away from being
quietly false, and nothing would say so.

This makes it a gate failure instead: any agent that allows conductor broadly must carry a
more-specific deny for `crp answer` and `crp withdraw`. Read-only, offline, stdlib-only.

The frontmatter is read with a small indentation-aware scan rather than a YAML dependency,
matching `agent_models.py` — the block is two levels deep and the shape is fixed.
"""

from __future__ import annotations

import re
from pathlib import Path

from conductor.projects import stack_root

# Deliberately the *commands*, not the tool names: an agent reaches CRPs through the CLI.
_FORBIDDEN = ("crp answer", "crp withdraw")
_BROAD = "python3 -m conductor"

_ASSIGN = re.compile(r"""^(["'])(?P<pattern>.*?)\1\s*:\s*(?P<verdict>allow|deny)\s*$""")


def agent_dirs() -> list[Path]:
    """Every place agent definitions live — the shared set and the loop's own."""
    root = stack_root()
    return [d for d in (root / "agents" / "definitions", root / "ops" / "agentloop") if d.is_dir()]


def bash_perms(path: Path) -> dict[str, str]:
    """`{pattern: allow|deny}` from `permission.bash` in the frontmatter. `{}` on any error."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}

    perms: dict[str, str] = {}
    in_permission = in_bash = False
    for line in text[3:end].splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 0:
            in_permission, in_bash = stripped.startswith("permission:"), False
            continue
        if in_permission and indent == 2:
            in_bash = stripped.startswith("bash:")
            continue
        if in_bash and indent >= 4:
            m = _ASSIGN.match(stripped)
            if m:
                perms[m.group("pattern")] = m.group("verdict")
    return perms


def violations(perms: dict[str, str]) -> list[str]:
    """The rule, as a pure function of one agent's bash permissions.

    Extracted so it can be tested exhaustively without a filesystem — the parsing and the
    rule fail in different ways, and only one of them needs a fixture.
    """
    broad = any(p.startswith(_BROAD) and p.endswith("*") and v == "allow" for p, v in perms.items())
    if not broad:
        return []
    return [
        f"allows `{_BROAD} *` but does not deny `{_BROAD} {command}`"
        for command in _FORBIDDEN
        if not any(p.startswith(f"{_BROAD} {command}") and v == "deny" for p, v in perms.items())
    ]


def report() -> list[str]:
    """One failure per agent that can reach a CRP's answer path. Never raises."""
    failures: list[str] = []
    for directory in agent_dirs():
        for path in sorted(directory.glob("*.md")):
            perms = bash_perms(path)
            for problem in violations(perms):
                failures.append(f"{path.name}: {problem} — an agent must not resolve its own CRP")
    return failures
