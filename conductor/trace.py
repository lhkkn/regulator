"""Tool-call traces — what did the agent actually call, and can we replay it?

The MCP surface is the agent's hands; `brief` shows the *state* but nothing records the
*calls*. When an agent does something surprising, "why?" starts with "what tools did it
call, with what, and what came back?". This is the append-only trace: every `tools/call`
through the MCP server is recorded (tool, redacted args, ok, duration, output size) and
can be listed, filtered, and **replayed** against today's reality.

Local-first: traces live in `~/Library/Application Support/conductor/traces.jsonl`,
never leave the machine, and secrets are never written (args are redacted; `reveal`
values are never in args). Append-only with a size cap.

    python -m conductor traces                 # last 20 calls
    python -m conductor traces -n 50 --tool keys
    python -m conductor traces --json
    python -m conductor traces --replay <id>   # re-run one recorded call, compare
"""

from __future__ import annotations

import json
import sys
import time
from datetime import UTC, datetime

from conductor import config
from conductor.redact import redact

_MAX_BYTES = 2_000_000
_KEEP = 2000


def _path():
    return config.state_file("traces.jsonl")


def record(tool: str, args: dict, *, ok: bool, ms: float, out_bytes: int = 0, error: str | None = None) -> None:
    """Append one call. Best-effort — tracing must never break a call."""
    entry = {
        "id": time.time_ns(),
        "ts": datetime.now(UTC).isoformat(),
        "tool": tool,
        "args": redact(args),
        "ok": ok,
        "ms": round(ms, 1),
        "out": out_bytes,
    }
    if error:
        entry["error"] = error[:200]
    try:
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > _MAX_BYTES:
            lines = path.read_text(encoding="utf-8").splitlines()
            path.write_text("\n".join(lines[-_KEEP:]) + "\n", encoding="utf-8")
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def load(limit: int = 20) -> list[dict]:
    """Most recent entries, newest last."""
    try:
        lines = _path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def render(entries: list[dict]) -> str:
    if not entries:
        return "traces: none recorded"
    lines = []
    for e in entries:
        ts = (e.get("ts") or "")[11:19]
        mark = "ok  " if e.get("ok") else "FAIL"
        args = json.dumps(e.get("args", {}), ensure_ascii=False)
        args = args if args != "{}" else ""
        tail = f"{e.get('ms', 0)}ms {e.get('out', 0)}b"
        if e.get("error"):
            tail += f" · {e['error']}"
        lines.append(f"  [{mark}] {ts} {e.get('tool')} {args}  ({tail})")
    return "\n".join(lines)


def replay(entry: dict) -> str:
    """Re-issue a recorded call and compare the result to the recorded outcome."""
    from conductor.mcp import server

    resp = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": entry["tool"], "arguments": entry.get("args", {})},
        }
    )
    result = (resp or {}).get("result", {})
    ok = not bool(result.get("isError"))
    text = "".join(c.get("text", "") for c in result.get("content", []) if isinstance(c, dict))
    same = ok == entry.get("ok")
    return (
        f"replay {entry['tool']} {json.dumps(entry.get('args', {}))}\n"
        f"  recorded: ok={entry.get('ok')} {entry.get('out', 0)}b\n"
        f"  now:      ok={ok} {len(text.encode())}b\n"
        f"  {'unchanged outcome' if same else 'OUTCOME CHANGED'}\n"
        f"--- current output ---\n{text[:600]}"
    )


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    limit = 20
    if "-n" in argv:
        try:
            limit = int(argv[argv.index("-n") + 1])
        except (IndexError, ValueError):
            pass
    entries = load(limit)

    if "--tool" in argv:
        needle = argv[argv.index("--tool") + 1].lower()
        entries = [e for e in entries if needle in (e.get("tool") or "").lower()]

    if "--replay" in argv:
        try:
            rid = int(argv[argv.index("--replay") + 1])
        except (IndexError, ValueError):
            print("usage: regulator traces --replay <id>")
            return 2
        match = next((e for e in load(10_000) if e.get("id") == rid), None)
        if not match:
            print(f"traces: no call with id {rid}")
            return 1
        print(replay(match))
        return 0

    if "--json" in argv:
        print(json.dumps(entries, ensure_ascii=False, indent=2))
        return 0
    print(render(entries))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
