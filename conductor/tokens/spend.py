"""Token-spend parsers (see module docstring in __init__.py). Read-only, defensive.

Record shape (what every parser yields):
    {day, source, model, input, output, cache_read, cache_write, usd, cost_kind}
      day        "YYYY-MM-DD" (local time)
      source     "kimi" | "claude" | "opencode"
      cost_kind  "actual" (source logged real cost) | "estimate" (pricing table)
                 | "plan" (managed subscription, $0 marginal) | "free" (local model)

Defensiveness contract: a missing directory, unreadable file, or malformed line NEVER
raises — it degrades to fewer records. Formats drift (flagged risk in plans/ui-magi.md);
the UI shows what it can and labels provenance.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

HOME = Path.home()
KIMI_SESSIONS = HOME / ".kimi-code" / "sessions"
CLAUDE_PROJECTS = HOME / ".claude" / "projects"
OPENCODE_DB = HOME / ".local" / "share" / "opencode" / "opencode.db"

# Per-1M-token USD estimates: (input, output). Cache read billed at READ_MULT x input,
# cache write at WRITE_MULT x input. Hand-maintained — goes stale; estimates only.
READ_MULT, WRITE_MULT = 0.1, 1.25
PRICING = {
    "claude-opus": (15.00, 75.00),
    "claude-sonnet": (3.00, 15.00),
    "claude-haiku": (1.00, 5.00),
    "kimi-k3": (0.60, 2.50),
    "kimi-k2": (0.60, 2.50),
    "kimi-for-coding": (0.60, 2.50),
}

MAX_FILE_BYTES = 64 * 1024 * 1024  # skip pathological logs; a day of records never needs one


def _day_from_ms(ms: float) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d")


def _day_from_iso(ts: str) -> str | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d")
    except (ValueError, AttributeError):
        return None


def _ts_from_iso(ts: str) -> float | None:
    try:
        return datetime.fromisoformat((ts or "").replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def _price(model: str, inp: int, out: int, cr: int, cw: int) -> float:
    m = (model or "").lower()
    for prefix, (pin, pout) in PRICING.items():
        if m.startswith(prefix):
            return (inp * pin + out * pout + cr * pin * READ_MULT + cw * pin * WRITE_MULT) / 1e6
    return 0.0


def _recent_files(root: Path, pattern: str, cutoff: float) -> list[Path]:
    """Glob `pattern` under root, keeping only files modified since `cutoff` (mtime filter
    is the perf guard — the full corpora are hundreds of MB)."""
    out = []
    try:
        for p in root.glob(pattern):
            try:
                st = p.stat()
                if st.st_mtime >= cutoff and st.st_size <= MAX_FILE_BYTES:
                    out.append(p)
            except OSError:
                continue
    except OSError:
        pass
    return out


def _kimi(cutoff: float) -> list[dict]:
    recs = []
    for f in _recent_files(KIMI_SESSIONS, "wd_*/session_*/agents/main/wire.jsonl", cutoff):
        try:
            with f.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"usage.record"' not in line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    scope = obj.get("usageScope")
                    if scope is not None and scope != "turn":
                        continue  # session-scope rollups would double-count turns
                    u = obj.get("usage") or {}
                    ts = obj.get("time")
                    if not isinstance(u, dict) or not ts:
                        continue
                    sess = next((p for p in f.parts if p.startswith("session_")), None)
                    recs.append(
                        {
                            "day": _day_from_ms(ts),
                            "ts": ts / 1000.0,
                            "source": "kimi",
                            "session": sess,
                            "project": None,
                            "agent": None,
                            "model": obj.get("model") or "kimi",
                            "input": int(u.get("inputOther") or 0),
                            "output": int(u.get("output") or 0),
                            "cache_read": int(u.get("inputCacheRead") or 0),
                            "cache_write": int(u.get("inputCacheCreation") or 0),
                            "usd": 0.0,
                            "cost_kind": "plan",
                        }
                    )
        except OSError:
            continue
    return recs


def _claude(cutoff: float) -> list[dict]:
    by_id: dict[str, dict] = {}  # a message id spans several transcript lines; last wins
    for f in _recent_files(CLAUDE_PROJECTS, "*/*.jsonl", cutoff):
        try:
            with f.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"usage"' not in line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    msg = obj.get("message") or {}
                    if msg.get("model") == "<synthetic>":
                        continue  # warmup rows, not real spend
                    u = msg.get("usage")
                    if not isinstance(u, dict):
                        continue
                    day = _day_from_iso(obj.get("timestamp", ""))
                    if day is None:
                        continue
                    inp = int(u.get("input_tokens") or 0)
                    out = int(u.get("output_tokens") or 0)
                    cr = int(u.get("cache_read_input_tokens") or 0)
                    cw = int(u.get("cache_creation_input_tokens") or 0)
                    model = msg.get("model") or "claude"
                    by_id[msg.get("id") or f"{f}:{day}:{inp}:{out}:{cr}:{cw}:{len(by_id)}"] = {
                        "day": day,
                        "ts": _ts_from_iso(obj.get("timestamp", "")),
                        "source": "claude",
                        "session": f.stem,
                        "project": f.parent.name,
                        "agent": None,
                        "model": model,
                        "input": inp,
                        "output": out,
                        "cache_read": cr,
                        "cache_write": cw,
                        "usd": _price(model, inp, out, cr, cw),
                        "cost_kind": "estimate",
                    }
        except OSError:
            continue
    return list(by_id.values())


def _opencode(cutoff: float) -> list[dict]:
    if not OPENCODE_DB.is_file():
        return []
    rich_sql = """SELECT m.time_created, m.session_id,
                         json_extract(m.data,'$.modelID'), json_extract(m.data,'$.providerID'),
                         json_extract(m.data,'$.tokens.input'), json_extract(m.data,'$.tokens.output'),
                         json_extract(m.data,'$.tokens.reasoning'),
                         json_extract(m.data,'$.tokens.cache.read'),
                         json_extract(m.data,'$.tokens.cache.write'),
                         json_extract(m.data,'$.cost'),
                         json_extract(m.data,'$.agent'), json_extract(m.data,'$.mode'),
                         s.directory, p.worktree, p.name
                    FROM message m
                    LEFT JOIN session s ON m.session_id = s.id
                    LEFT JOIN project p ON s.project_id = p.id
                   WHERE m.time_created >= ? AND json_extract(m.data,'$.role') = 'assistant'"""
    basic_sql = """SELECT time_created,
                          json_extract(data,'$.modelID'), json_extract(data,'$.providerID'),
                          json_extract(data,'$.tokens.input'), json_extract(data,'$.tokens.output'),
                          json_extract(data,'$.tokens.reasoning'),
                          json_extract(data,'$.tokens.cache.read'),
                          json_extract(data,'$.tokens.cache.write'),
                          json_extract(data,'$.cost')
                     FROM message
                    WHERE time_created >= ? AND json_extract(data,'$.role') = 'assistant'"""
    recs = []
    db = None
    try:
        db = sqlite3.connect(f"file:{OPENCODE_DB}?mode=ro", uri=True, timeout=10)
        db.execute("PRAGMA busy_timeout = 10000")  # wait out a peer's write/checkpoint
        try:  # session/project join gives project + agent context
            rows = db.execute(rich_sql, (int(cutoff * 1000),)).fetchall()
            rich = True
        except sqlite3.Error:  # older/dbless fixtures: fall back to the basic query
            rows = db.execute(basic_sql, (int(cutoff * 1000),)).fetchall()
            rich = False
    finally:
        if db is not None:
            db.close()
    for row in rows:
        if rich:
            (
                ts,
                session_id,
                model,
                provider,
                inp,
                out,
                reasoning,
                cr,
                cw,
                cost,
                agent,
                mode,
                directory,
                worktree,
                projname,
            ) = row
        else:
            ts, model, provider, inp, out, reasoning, cr, cw, cost = row
            session_id = agent = mode = directory = worktree = projname = None
        if not ts:
            continue
        inp, out = int(inp or 0), int(out or 0) + int(reasoning or 0)
        cr, cw = int(cr or 0), int(cw or 0)
        if inp + out + cr + cw == 0 and not cost:
            continue  # placeholder row written before the call completed
        model = model or "unknown"
        if cost:
            usd, kind = float(cost), "actual"
        elif (provider or "").startswith(("ollama", "lmstudio", "local")):
            usd, kind = 0.0, "free"
        else:
            usd, kind = _price(model, inp, out, cr, cw), "estimate"
        base = projname or worktree or directory
        recs.append(
            {
                "day": _day_from_ms(ts),
                "ts": ts / 1000.0,
                "source": "opencode",
                "session": session_id,
                "project": (Path(base).name if base else None),
                "agent": agent or mode,
                "model": model,
                "input": inp,
                "output": out,
                "cache_read": cr,
                "cache_write": cw,
                "usd": usd,
                "cost_kind": kind,
            }
        )
    return recs


SOURCES = {"kimi": _kimi, "claude": _claude, "opencode": _opencode}


def sources() -> dict[str, bool]:
    """Which spend sources exist on this machine (for the UI's provenance labels)."""
    return {
        "kimi": KIMI_SESSIONS.is_dir(),
        "claude": CLAUDE_PROJECTS.is_dir(),
        "opencode": OPENCODE_DB.is_file(),
    }


_MEMO: dict[int, tuple[float, list[dict]]] = {}
_MEMO_TTL = 120  # parsing the corpora costs ~2s; memoize so daily()+summary() share one pass
_MEMO_LOCK = threading.Lock()


def records(days: int = 30) -> list[dict]:
    with _MEMO_LOCK:
        hit = _MEMO.get(days)
        if hit and time.time() - hit[0] < _MEMO_TTL:
            return hit[1]
    cutoff = time.time() - days * 86400
    recs: list[dict] = []
    partial = False
    for fn in SOURCES.values():
        try:
            recs.extend(fn(cutoff))
        except Exception:
            partial = True  # a source failed (e.g. a locked DB) — one bad source never kills the rest
    present = any(sources().values())
    with _MEMO_LOCK:
        h = _MEMO.get(days)
        if partial or not recs:
            if h:
                return h[1]  # a partial/empty/absent parse never sticks — prefer the last good
            if not present:
                return []  # no sources on this machine → legitimately empty
            # sources exist but we parsed nothing: a transient block. Raise so the caller
            # (collector.get) serves the last good payload instead of caching emptiness.
            raise RuntimeError("spend: sources present but no records parsed")
        _MEMO[days] = (time.time(), recs)
    return recs


def _rollup(recs: list[dict], dim: str) -> list[dict]:
    """Per-day x <dim> rollup. `dim` is the record field to group by ("source" or
    "model"); the output record carries that field by name. Tokens summed; usd summed."""
    agg: dict[tuple[str, str], dict] = {}
    for r in recs:
        key = (r["day"], r[dim])
        a = agg.setdefault(
            key,
            {
                "day": r["day"],
                dim: r[dim],
                "input": 0,
                "output": 0,
                "cache_read": 0,
                "cache_write": 0,
                "usd": 0.0,
                "calls": 0,
            },
        )
        for k in ("input", "output", "cache_read", "cache_write"):
            a[k] += r[k]
        a["usd"] += r["usd"]
        a["calls"] += 1
    return sorted(agg.values(), key=lambda a: (a["day"], a[dim]))


def daily(days: int = 30) -> list[dict]:
    """Per-day x source rollup, ascending. Tokens summed; usd summed (mixed kinds)."""
    return _rollup(records(days), "source")


def daily_by_model(days: int = 30) -> list[dict]:
    """Per-day x model rollup (parallel to daily()). Same record shape, keyed by model."""
    return _rollup(records(days), "model")


def requests(days: int = 30, limit: int = 500, only_paid: bool = False) -> list[dict]:
    """Per-request records, newest first — prompt-level granularity (no prompt text).
    Fields: ts, day, source, session, project, agent, model, tokens, usd, cost_kind."""
    recs = [r for r in records(days) if r.get("ts")]
    if only_paid:
        recs = [r for r in recs if r["usd"] > 0]
    recs.sort(key=lambda r: r["ts"], reverse=True)
    return recs[:limit]


def _rollup_dim(recs: list[dict], dim: str) -> list[dict]:
    agg: dict[str, dict] = {}
    for r in recs:
        k = r.get(dim) or "—"
        a = agg.setdefault(k, {dim: k, "usd": 0.0, "calls": 0, "input": 0, "output": 0})
        a["usd"] += r["usd"]
        a["calls"] += 1
        a["input"] += r["input"]
        a["output"] += r["output"]
    return sorted(agg.values(), key=lambda a: -a["usd"])


def by_project(days: int = 30) -> list[dict]:
    """Spend per project (opencode carries cwd; others '—')."""
    return _rollup_dim(records(days), "project")


def by_agent(days: int = 30) -> list[dict]:
    """Spend per agent/mode (opencode carries agent+mode)."""
    return _rollup_dim(records(days), "agent")


def top(days: int = 30, n: int = 10) -> list[dict]:
    """The most expensive requests (per-call)."""
    recs = sorted((r for r in records(days) if r.get("ts")), key=lambda r: -r["usd"])
    return recs[:n]


def percentiles(days: int = 30) -> dict:
    xs = sorted(r["usd"] for r in records(days) if r["usd"] > 0)
    if not xs:
        return {"median": 0.0, "p95": 0.0, "max": 0.0, "paid_calls": 0}

    def q(p: float) -> float:
        return xs[min(len(xs) - 1, int(p * len(xs)))]

    return {"median": round(q(0.5), 4), "p95": round(q(0.95), 4), "max": round(xs[-1], 4), "paid_calls": len(xs)}


def by_hour(days: int = 30) -> list[dict]:
    """Spend per hour-of-day (0..23) — when in the day the money goes."""
    agg: dict[int, dict] = {}
    for r in records(days):
        ts = r.get("ts")
        if not ts:
            continue
        h = datetime.fromtimestamp(ts).hour
        a = agg.setdefault(h, {"hour": h, "usd": 0.0, "calls": 0, "input": 0, "output": 0})
        a["usd"] += r["usd"]
        a["calls"] += 1
        a["input"] += r["input"]
        a["output"] += r["output"]
    return [agg.get(h, {"hour": h, "usd": 0.0, "calls": 0, "input": 0, "output": 0}) for h in range(24)]


def summary(days: int = 30) -> dict:
    """Totals by source and model + grand totals + today's figure (for KPI tiles)."""
    recs = records(days)
    by_source: dict[str, dict] = {}
    by_model: dict[str, dict] = {}
    today = datetime.now().strftime("%Y-%m-%d")
    tot_usd = tot_today = 0.0
    kinds: set[str] = set()
    for r in recs:
        kinds.add(r["cost_kind"])
        for table, key in ((by_source, r["source"]), (by_model, r["model"])):
            t = table.setdefault(key, {"input": 0, "output": 0, "usd": 0.0, "calls": 0, "cost_kind": r["cost_kind"]})
            t["input"] += r["input"]
            t["output"] += r["output"]
            t["usd"] += r["usd"]
            t["calls"] += 1
        tot_usd += r["usd"]
        if r["day"] == today:
            tot_today += r["usd"]
    return {
        "days": days,
        "total_usd": round(tot_usd, 4),
        "today_usd": round(tot_today, 4),
        "by_source": by_source,
        "by_model": by_model,
        "cost_kinds": sorted(kinds),
        "sources_present": sources(),
        "records": len(recs),
    }
