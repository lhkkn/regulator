"""Living project state — Track B1.

Auto-generates the *discoverable* parts of a project's `.context/state.md` (recent
commits, open GitHub issues) inside a banner-tagged block, preserving the curated
content outside it. Mirrors the registry's generated-block pattern
(`<!-- BEGIN GENERATED: ... -->`) so nothing hand-written gets clobbered.

    python3 -m conductor state [path]

Stdlib-only. Reads git and gh live; degrades gracefully when either is unavailable
or the path isn't a repository — the block always renders, never crashes. Discover,
don't hand-maintain.
"""

from __future__ import annotations

import json
import subprocess
from datetime import date
from pathlib import Path

BEGIN = "<!-- BEGIN generated: conductor context state -->"
END = "<!-- END generated: conductor context state -->"

NOT_REPO = "_(not a git repository — `git init` to start tracking)_"
GH_UNAVAILABLE = "_(gh unavailable or no GitHub remote)_"


def _run(cmd: list[str], cwd: Path) -> tuple[int, str]:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=15)
    return proc.returncode, proc.stdout.strip()


def recent_commits(path: Path, n: int = 10) -> str:
    rc, out = _run(["git", "log", f"-n{n}", "--format=%h %ad %s", "--date=short"], path)
    if rc != 0:
        return NOT_REPO
    lines = [f"- {line}" for line in out.splitlines() if line.strip()]
    return "\n".join(lines) if lines else "_(no commits yet)_"


def open_issues(path: Path, n: int = 10) -> str:
    try:
        rc, out = _run(
            ["gh", "issue", "list", "--state", "open", "--limit", str(n), "--json", "number,title"],
            path,
        )
    except FileNotFoundError:
        return GH_UNAVAILABLE
    if rc != 0:
        return GH_UNAVAILABLE
    try:
        items = json.loads(out) if out else []
    except json.JSONDecodeError:
        return GH_UNAVAILABLE
    if not items:
        return "_(none)_"
    return "\n".join(f"- #{it['number']} {it['title']}" for it in items)


def render_block(path: Path) -> str:
    today = date.today().isoformat()
    body = [
        BEGIN,
        f"_Last generated: {today} by `python3 -m conductor state`._",
        "",
        "### Recent commits",
        recent_commits(path),
        "",
        "### Open issues",
        open_issues(path),
        END,
    ]
    return "\n".join(body) + "\n"


def update_state_file(path: Path) -> Path:
    path = path.resolve()
    state_path = path / ".context" / "state.md"
    block = render_block(path)

    if not state_path.is_file():
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(f"# State\n\n{block}\n", encoding="utf-8")
        return state_path

    text = state_path.read_text(encoding="utf-8")
    if BEGIN in text and END in text:
        pre = text[: text.index(BEGIN)]
        post = text[text.index(END) + len(END) :]
        text = f"{pre}{block}{post}"
    else:
        lines = text.splitlines()
        insert_at = 1
        lines.insert(insert_at, "")
        lines.insert(insert_at + 1, block)
        text = "\n".join(lines)
    state_path.write_text(text, encoding="utf-8")
    return state_path


# conductor/context/state.py -> parents[2] = the project root (Stack/control).
DEFAULT_PROJECT = Path(__file__).resolve().parents[2]


def main(argv: list[str]) -> int:
    path = Path(argv[2]).resolve() if len(argv) > 2 else DEFAULT_PROJECT
    state_path = update_state_file(path)
    print(f"Updated {state_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(["", "state"]))
