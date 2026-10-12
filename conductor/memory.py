"""`conductor memory` — durable memory the stack carries across sessions.

A small SQLite store of facts, decisions, and notes that should outlive a single
session and be available to every agent. `context.build` folds in the most recent
memories. Writes are explicit (`conductor memory add`); reads are cheap.

    python3 -m conductor memory add "conductor owns the registry" --kind decision --tags arch
    python3 -m conductor memory search registry
    python3 -m conductor memory list
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sqlite3
import time
from pathlib import Path

from conductor import config
from conductor.projects import workspace_root

_SCHEMA = """CREATE TABLE IF NOT EXISTS memories(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'fact',
  text TEXT NOT NULL,
  tags TEXT NOT NULL DEFAULT '');"""


def db_path() -> Path:
    env = os.environ.get("CONDUCTOR_MEMORY_DB")
    if env:
        return Path(env).expanduser()
    return (workspace_root() or config.app_dir()) / ".memory.db"


def connect() -> sqlite3.Connection:
    db_path().parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(db_path())
    c.row_factory = sqlite3.Row
    return c


@contextlib.contextmanager
def _conn():
    """A connection that commits on success and **closes** on exit.

    `with sqlite3.connect(...)` manages the *transaction* but never closes the
    connection — a per-call leak in a long-running process (the MCP server folds memory
    into every `context.build`). This closes it too.
    """
    c = connect()
    try:
        with c:
            yield c
    finally:
        c.close()


def init() -> None:
    with _conn() as c:
        c.executescript(_SCHEMA)


def add(text: str, kind: str = "fact", tags: str = "") -> None:
    init()
    with _conn() as c:
        c.execute(
            "INSERT INTO memories(ts, kind, text, tags) VALUES(?,?,?,?)",
            (time.strftime("%Y-%m-%d %H:%M"), kind, text.strip(), tags.strip()),
        )


def search(q: str, limit: int = 20) -> list[dict]:
    init()
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM memories WHERE text LIKE ? OR tags LIKE ? ORDER BY id DESC LIMIT ?",
            (f"%{q}%", f"%{q}%", limit),
        ).fetchall()
    return [dict(r) for r in rows]


def recent(limit: int = 20) -> list[dict]:
    init()
    with _conn() as c:
        rows = c.execute("SELECT * FROM memories ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def render(rows: list[dict]) -> str:
    if not rows:
        return "(none)"
    return "\n".join(
        f"  [{r['ts']}] ({r['kind']}) {r['text']}" + (f"  #{r['tags']}" if r["tags"] else "") for r in rows
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="regulator memory", description="durable cross-session memory")
    sub = ap.add_subparsers(dest="cmd")
    a = sub.add_parser("add")
    a.add_argument("text")
    a.add_argument("--kind", default="fact")
    a.add_argument("--tags", default="")
    s = sub.add_parser("search")
    s.add_argument("query")
    lst = sub.add_parser("list")
    lst.add_argument("-n", type=int, default=20)
    args = ap.parse_args(argv)

    if args.cmd == "add":
        add(args.text, args.kind, args.tags)
        print(f"remembered: {args.text[:70]}")
        return 0
    if args.cmd == "search":
        print(render(search(args.query)))
        return 0
    print(render(recent(getattr(args, "n", 20))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
