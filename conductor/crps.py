"""`conductor crp` — the Consultation Request Pack: a durable question from agent to human.

An agent that hits ambiguity it must not resolve alone **files a CRP instead of guessing**.
The CRP outlives the session, queues where the human already looks (`conductor brief`), and
carries a `default_action` — so an unattended run *declares* what it will do rather than
inventing it silently.

The asymmetry is the whole mechanism: **agents file, humans answer.** `crp_file` is an MCP
tool (write-class, governed by an actuator contract in `conductor/contracts.py`). Answering
is CLI-only — deliberately *not* an MCP tool, so no agent can resolve its own question.

    python3 -m conductor crp list
    python3 -m conductor crp show 3
    python3 -m conductor crp answer 3 --choose "split them" --why "cheaper to revert"
    python3 -m conductor crp withdraw 3

Design notes:
  * Stored in the workspace root (`.crp.db`), beside `.memory.db` — or in the app dir
    when no workspace is configured. Override with `$CONDUCTOR_CRP_DB` (tests use a temp file).
  * `default_after` is a deadline, not a timer: nothing fires it yet. Defining the
    contract now and the executor later keeps a stale question preferable to a lost one.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from conductor import config
from conductor.projects import workspace_root

OPEN = "open"
ANSWERED = "answered"
EXPIRED = "expired"
WITHDRAWN = "withdrawn"

# Spam guard: an agent looping on new ambiguities gets a bounded number of slots, so a
# confused run cannot bury the queue. Enforced in the `crp.file` precondition.
MAX_OPEN_PER_ACTOR = 5

_SCHEMA = """CREATE TABLE IF NOT EXISTS crps(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  actor TEXT NOT NULL DEFAULT 'cli',
  project TEXT NOT NULL DEFAULT '',
  question TEXT NOT NULL,
  options TEXT NOT NULL DEFAULT '[]',
  context TEXT NOT NULL DEFAULT '',
  blast_radius TEXT NOT NULL DEFAULT '',
  default_action TEXT NOT NULL DEFAULT '',
  default_after TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'open',
  answer TEXT,
  answered_ts TEXT);"""


def db_path() -> Path:
    env = os.environ.get("CONDUCTOR_CRP_DB")
    if env:
        return Path(env).expanduser()
    return (workspace_root() or config.app_dir()) / ".crp.db"


def connect() -> sqlite3.Connection:
    db_path().parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(db_path())
    c.row_factory = sqlite3.Row
    return c


@contextlib.contextmanager
def _conn():
    """Commit on success, **close** on exit — `with sqlite3.connect(...)` leaks the
    connection in a long-running process (the MCP server files CRPs). Same reasoning as
    `memory._conn`."""
    c = connect()
    try:
        with c:
            yield c
    finally:
        c.close()


def init() -> None:
    with _conn() as c:
        c.executescript(_SCHEMA)


def _normalise_options(options: list | None) -> list[dict]:
    """Accept `["a", "b"]` or `[{"label": ..., "tradeoff": ...}]` — always store the latter."""
    out: list[dict] = []
    for o in options or []:
        if isinstance(o, str):
            out.append({"label": o.strip(), "tradeoff": ""})
        elif isinstance(o, dict) and o.get("label"):
            out.append({"label": str(o["label"]).strip(), "tradeoff": str(o.get("tradeoff", "")).strip()})
    return out


class Refused(Exception):
    """A CRP that must not be filed. Carries the reason, for the human or the agent."""


def check(question: str, *, actor: str = "cli", default_after: str = "") -> tuple[bool, str]:
    """The one rule, shared by **every** entry point: the CLI, the MCP contract, and `file()`.

    Keeping this in one place is the whole point. The agentloop chore agent reaches CRPs
    through the CLI — `python3 -m conductor *` is on its bash allowlist — so a guard that
    lived only in the actuator contract would be silently bypassed by the very caller it
    was written to bound. A rule enforced on one path is a rule that will be walked around.
    """
    q = (question or "").strip()
    if not q:
        return False, "a CRP needs a question"
    ok, why = parse_default_after(default_after)
    if not ok:
        return False, why
    if is_duplicate_open(q):
        return False, "the same question is already open — answer or withdraw that one first"
    who = (actor or "cli").strip() or "cli"
    queued = open_count(who)
    if queued >= MAX_OPEN_PER_ACTOR:
        return False, (
            f"{who} already has {queued} open CRPs (max {MAX_OPEN_PER_ACTOR}) — "
            "a confused run must not be able to bury the queue"
        )
    return True, f"bounded, not already queued, and {who} is under quota"


def file(  # noqa: A001 - the verb is the domain word here (“file a CRP”)
    question: str,
    *,
    options: list | None = None,
    context: str = "",
    blast_radius: str = "",
    default_action: str = "",
    default_after: str = "",
    actor: str = "cli",
    project: str = "",
) -> int:
    """Insert a CRP and return its id. Raises `Refused` if `check()` says no.

    The guard runs here too, not only in the contract, so no caller can skip it by
    choosing a different door.
    """
    ok, why = check(question, actor=actor, default_after=default_after)
    if not ok:
        raise Refused(why)
    init()
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO crps(ts, actor, project, question, options, context, blast_radius,"
            " default_action, default_after, status) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                time.strftime("%Y-%m-%d %H:%M"),
                actor.strip() or "cli",
                project.strip(),
                question.strip(),
                json.dumps(_normalise_options(options), ensure_ascii=False),
                context.strip(),
                blast_radius.strip(),
                default_action.strip(),
                default_after.strip(),
                OPEN,
            ),
        )
        return int(cur.lastrowid or 0)


def get(crp_id: int) -> dict | None:
    init()
    with _conn() as c:
        row = c.execute("SELECT * FROM crps WHERE id = ?", (crp_id,)).fetchone()
    return dict(row) if row else None


def open_crps() -> list[dict]:
    """Every CRP awaiting a human, oldest first — a queue is answered in order."""
    init()
    with _conn() as c:
        rows = c.execute("SELECT * FROM crps WHERE status = ? ORDER BY id", (OPEN,)).fetchall()
    return [dict(r) for r in rows]


def answer(crp_id: int, choice: str, rationale: str = "") -> bool:
    """Answer an open CRP. False if it does not exist or was already resolved.

    CLI-only by design — see the module docstring.
    """
    init()
    with _conn() as c:
        cur = c.execute(
            "UPDATE crps SET status = ?, answer = ?, answered_ts = ? WHERE id = ? AND status = ?",
            (
                ANSWERED,
                json.dumps({"choice": choice.strip(), "rationale": rationale.strip()}, ensure_ascii=False),
                time.strftime("%Y-%m-%d %H:%M"),
                crp_id,
                OPEN,
            ),
        )
        return cur.rowcount == 1


def withdraw(crp_id: int) -> bool:
    """Take back an open CRP — the actuator contract's rollback."""
    init()
    with _conn() as c:
        cur = c.execute("UPDATE crps SET status = ? WHERE id = ? AND status = ?", (WITHDRAWN, crp_id, OPEN))
        return cur.rowcount == 1


def is_duplicate_open(question: str) -> bool:
    """True if the same question is already awaiting an answer (precondition guard)."""
    init()
    with _conn() as c:
        row = c.execute(
            "SELECT 1 FROM crps WHERE status = ? AND lower(trim(question)) = lower(trim(?)) LIMIT 1",
            (OPEN, question),
        ).fetchone()
    return row is not None


def _opt_lines(raw: str) -> list[str]:
    try:
        opts = json.loads(raw or "[]")
    except ValueError:
        return []
    return [
        f"      {chr(97 + i)}) {o.get('label', '')}" + (f" — {o['tradeoff']}" if o.get("tradeoff") else "")
        for i, o in enumerate(opts)
    ]


def render(rows: list[dict], *, verbose: bool = False) -> str:
    """Render a queue. `verbose` (used by `crp list`/`show`) adds the decision context."""
    if not rows:
        return "  (no open CRPs)"
    out: list[str] = []
    for r in rows:
        age = ""
        if r.get("default_after"):
            age = f"  → default fires after {r['default_after']}"
        out.append(f"  #{r['id']} [{r['ts']}] {r['actor']}{'/' + r['project'] if r['project'] else ''}{age}")
        out.append(f"      {r['question']}")
        out.extend(_opt_lines(r.get("options", "")))
        if verbose:
            for label, key in (("context", "context"), ("blast radius", "blast_radius"), ("default", "default_action")):
                if r.get(key):
                    out.append(f"      {label}: {r[key]}")
        if r.get("status") == ANSWERED and r.get("answer"):
            try:
                a = json.loads(r["answer"])
                out.append(
                    f"      answered: {a.get('choice', '')}" + (f" — {a['rationale']}" if a.get("rationale") else "")
                )
            except ValueError:
                pass
    return "\n".join(out)


def summary() -> dict:
    """What `brief` needs: the open queue, its size, and any lapsed default.

    Expiry runs here so the digest is the place a lapsed deadline surfaces — the watcher
    calls `expire_due` too, but `brief` must not depend on the watcher having run.
    """
    try:
        expire_due()
        rows = open_crps()
        gone = lapsed()
    except Exception:  # noqa: BLE001 - brief must never fail because of this
        return {"open": 0, "rows": [], "lapsed": 0, "lapsed_rows": []}
    return {"open": len(rows), "rows": rows, "lapsed": len(gone), "lapsed_rows": gone}


def parse_default_after(value: str) -> tuple[bool, str]:
    """A past or unparseable deadline would fire immediately — refuse it up front."""
    if not value:
        return True, ""
    when = _parse_when(value)
    if when is None:
        return False, f"default_after must be YYYY-MM-DD or 'YYYY-MM-DD HH:MM' (got {value!r})"
    if when < datetime.now():
        return False, f"default_after is in the past: {value}"
    return True, ""


def _parse_when(value: str) -> datetime | None:
    """Parse a deadline without judging it — expiry needs past dates to be valid input."""
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def expire_due(now: datetime | None = None) -> list[dict]:
    """Mark open CRPs whose `default_after` has passed as `expired`, and return them.

    This is deliberately **all** it does. Conductor records that the deadline lapsed and
    surfaces it; it does not execute `default_action`. Running an arbitrary declared
    action unattended is precisely the authority the human side exists to withhold — so
    the agent that filed the CRP resumes, sees its own declared default is now in effect,
    and acts on that with the fact on the record.
    """
    init()
    now = now or datetime.now()
    lapsed = [
        row for row in open_crps() if (when := _parse_when(row.get("default_after") or "")) is not None and when <= now
    ]
    for row in lapsed:
        with _conn() as c:
            c.execute("UPDATE crps SET status = ? WHERE id = ? AND status = ?", (EXPIRED, row["id"], OPEN))
    return lapsed


def lapsed() -> list[dict]:
    """CRPs whose deadline passed unanswered — their declared defaults are now in effect."""
    init()
    with _conn() as c:
        rows = c.execute("SELECT * FROM crps WHERE status = ? ORDER BY id", (EXPIRED,)).fetchall()
    return [dict(r) for r in rows]


def open_count(actor: str = "") -> int:
    """Open CRPs, optionally scoped to one actor — the input to the spam guard."""
    init()
    with _conn() as c:
        if actor:
            row = c.execute("SELECT COUNT(*) FROM crps WHERE status = ? AND actor = ?", (OPEN, actor)).fetchone()
        else:
            row = c.execute("SELECT COUNT(*) FROM crps WHERE status = ?", (OPEN,)).fetchone()
    return int(row[0] if row else 0)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="regulator crp", description="Consultation Request Packs")
    sub = ap.add_subparsers(dest="cmd")

    f = sub.add_parser("file", help="file a question (agents do this via the MCP tool)")
    f.add_argument("question")
    f.add_argument("--option", action="append", default=[], help="repeatable: 'label: tradeoff'")
    f.add_argument("--context", default="")
    f.add_argument("--blast-radius", default="")
    f.add_argument("--default", dest="default_action", default="")
    f.add_argument("--default-after", default="")
    f.add_argument("--project", default="")

    lst = sub.add_parser("list", help="open CRPs, oldest first")
    lst.add_argument("-n", type=int, default=20)

    show = sub.add_parser("show", help="one CRP with its full decision context")
    show.add_argument("id", type=int)

    ans = sub.add_parser("answer", help="answer an open CRP (human surface — not an MCP tool)")
    ans.add_argument("id", type=int)
    ans.add_argument("--choose", required=True)
    ans.add_argument("--why", default="")

    wd = sub.add_parser("withdraw", help="take back an open CRP")
    wd.add_argument("id", type=int)

    args = ap.parse_args(argv)

    if args.cmd == "file":
        opts = []
        for o in args.option:
            label, _, tradeoff = o.partition(":")
            opts.append({"label": label.strip(), "tradeoff": tradeoff.strip()})
        try:
            cid = file(
                args.question,
                options=opts,
                context=args.context,
                blast_radius=args.blast_radius,
                default_action=args.default_action,
                default_after=args.default_after,
                project=args.project,
                # An agent identifies itself via $CONDUCTOR_ACTOR and gets its own quota
                # bucket; a human falls through to "cli".
                actor=os.environ.get("CONDUCTOR_ACTOR", "cli"),
            )
        except Refused as exc:
            print(f"refused: {exc}")
            return 2
        print(f"filed CRP #{cid}")
        return 0

    if args.cmd == "show":
        row = get(args.id)
        if row is None:
            print(f"no such CRP: {args.id}")
            return 1
        print(render([row], verbose=True))
        return 0

    if args.cmd == "answer":
        if answer(args.id, args.choose, args.why):
            print(f"CRP #{args.id} answered: {args.choose}")
            return 0
        print(f"refused: CRP #{args.id} is not open (missing, or already resolved)")
        return 1

    if args.cmd == "withdraw":
        if withdraw(args.id):
            print(f"withdrew CRP #{args.id}")
            return 0
        print(f"refused: CRP #{args.id} is not open")
        return 1

    rows = open_crps()[: getattr(args, "n", 20)]
    print(render(rows, verbose=True) if rows else "(no open CRPs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
