"""Action ledger — the append-only, hash-chained record of applied actions.

Sensors record state; actuators must record *effects*. Every action the actuator runs
(precondition → act → verify → rollback/flag) is appended here with who did it, what it
touched, whether the verifier passed, and how to undo it. Entries are hash-chained (each
entry commits to the previous one), so a tampered or truncated history is detectable —
the ledger is evidence, not just a log.

**Durability.** The chain never resets. When the active file grows past `_CAP` it is
*moved* to `<app>/ledger.d/<first-seq>.jsonl` and a fresh active file starts — but the
sequence numbers and `prev` links continue across segments, so the concatenation of
segments is still one continuous chain. `verify` walks every segment in order: deleting
or editing any entry (active or archived) shows up as a sequence gap or a hash mismatch.

Local-first: `<app>/ledger.jsonl` (+ segments under `<app>/ledger.d/`). Redacted at
append — secret-looking fields are masked and long strings capped, so a secret can never
land in the history. Best-effort: ledgering must never break the action it records.

    python -m conductor ledger            # recent entries + chain integrity
    python -m conductor ledger --verify   # chain integrity only (exit 1 if broken)
    python -m conductor ledger --json
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import UTC, datetime

from conductor import config
from conductor.redact import redact

_GENESIS = "0" * 64  # the `prev` of the first entry
_CAP = 4_000_000  # bytes per active segment before rotation
_MAX_STR = 500


def path():
    """The active ledger file (a function so CONDUCTOR_APP_DIR redirects still apply)."""
    return config.state_file("ledger.jsonl")


def segment_dir():
    """Where rotated segments live."""
    return path().parent / "ledger.d"


def _canonical(payload: dict) -> str:
    """Deterministic JSON — the byte string the hash commits to."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(prev: str, core: dict) -> str:
    return hashlib.sha256((prev + _canonical(core)).encode("utf-8")).hexdigest()


def _read_segment(p) -> list[dict]:
    """Parseable entries in one file, oldest first (unparseable lines are skipped and thus
    break the chain, which `verify` reports)."""
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return []
    entries = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    return entries


def _segments() -> list:
    """Every segment file, oldest first: archived (`ledger.d/*.jsonl`, name-sorted) then
    the active file."""
    d = segment_dir()
    archived = sorted(d.glob("*.jsonl")) if d.is_dir() else []
    active = path()
    return [*archived, active] if active.exists() else list(archived)


def _all_entries() -> list[dict]:
    """The whole chain, across every segment, oldest first."""
    return [e for p in _segments() for e in _read_segment(p)]


def load(limit: int | None = None) -> list[dict]:
    """Recent entries, newest last."""
    entries = _all_entries()
    return entries[-limit:] if limit else entries


def _rotate(entries: list[dict]) -> None:
    """Move the active file aside as a segment named for its first seq."""
    dest_dir = segment_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    path().replace(dest_dir / f"{int(entries[0].get('seq', 0)):08d}.jsonl")


def append(
    *,
    tool: str,
    action: str,
    args: dict,
    precondition,
    effect,
    verify,
    outcome: str,
    rollback: str | None = None,
    actor: str | None = None,
) -> dict:
    """Append one action to the chain. Returns the entry. Best-effort — never raises.

    `seq` is global (it keeps counting across rotated segments) and `prev` is the previous
    entry's hash (genesis for the first), so any later edit of any field — or deletion of a
    segment — breaks the chain.
    """
    prior = _all_entries()
    seq = len(prior)
    prev = prior[-1].get("hash", _GENESIS) if prior else _GENESIS
    core = {
        "seq": seq,
        "ts": datetime.now(UTC).isoformat(),
        "actor": actor or "unknown",
        "tool": tool,
        "action": action,
        "args": redact(args or {}, max_str=_MAX_STR),
        "precondition": redact(precondition, max_str=_MAX_STR) if precondition is not None else None,
        "effect": redact(effect, max_str=_MAX_STR) if effect is not None else None,
        "verify": redact(verify, max_str=_MAX_STR) if verify is not None else None,
        "outcome": outcome,
        "rollback": rollback,
    }
    try:
        entry = {**core, "prev": prev}
        entry["hash"] = _digest(prev, core)
    except (TypeError, ValueError):
        return {}  # a non-serializable payload is dropped, never raised
    try:
        p = path()
        p.parent.mkdir(parents=True, exist_ok=True)
        active = _read_segment(p) if p.exists() else []
        # Rotate the active segment when it's already past the cap (the new entry then
        # starts a fresh file; seq/prev continue, so the chain is unbroken).
        if p.exists() and active and p.stat().st_size > _CAP:
            _rotate(active)
        # A torn trailing line (no newline) would swallow the next append; guard it.
        if p.exists() and p.stat().st_size:
            tail = p.read_bytes()[-1:]
            if tail and tail != b"\n":
                with p.open("ab") as fh:
                    fh.write(b"\n")
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except (OSError, TypeError, ValueError):
        pass
    return entry


def verify_entries(entries: list[dict]) -> tuple[bool, str]:
    """Check sequence continuity, back-links and hashes. Returns (ok, detail)."""
    prev = _GENESIS
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            return False, f"entry {i} is not an object"
        if entry.get("seq") != i:
            return False, f"entry {i} has seq {entry.get('seq')} (expected {i}) — gap or reorder"
        if entry.get("prev") != prev:
            return False, f"entry {i} prev-link broken (expected {prev[:12]}…, got {str(entry.get('prev'))[:12]}…)"
        core = {k: v for k, v in entry.items() if k not in ("prev", "hash")}
        if entry.get("hash") != _digest(prev, core):
            return False, f"entry {i} hash mismatch — content altered"
        prev = entry["hash"]
    return True, f"{len(entries)} entr{'y' if len(entries) == 1 else 'ies'} chained"


def verify() -> tuple[bool, str]:
    """Verify the whole on-disk chain (all segments). Missing ledger is trivially intact."""
    entries = _all_entries()
    if not entries:
        return True, "no ledger yet"
    return verify_entries(entries)


def render(entries: list[dict], verified: tuple[bool, str] | None = None) -> str:
    if not _segments():
        return "ledger: no entries yet"
    lines = []
    if verified is not None:
        ok, detail = verified
        lines.append(f"ledger integrity: {'ok' if ok else 'BROKEN'} — {detail}")
    marks = {
        "verified": "ok  ",
        "rolled_back": "ROLL",
        "flagged": "FLAG",
        "failed": "FAIL",
        "refused": "ref ",
        "dry-run": "dry ",
    }
    for e in entries:
        ts = (e.get("ts") or "")[11:19]
        outcome = e.get("outcome") or "unknown"
        mark = marks.get(outcome, "????")
        pre = e.get("precondition") or {}
        tail = e.get("rollback") or ""
        show_rollback = bool(tail) and outcome in ("rolled_back", "flagged", "failed")
        lines.append(
            f"  [{mark}] {ts} #{e.get('seq')} {e.get('action')} by {e.get('actor')} "
            f"({outcome}"
            + (f"; pre {'ok' if pre.get('ok') else 'no'}" if pre else "")
            + (f"; rollback: {tail}" if show_rollback else "")
            + ")"
        )
    return "\n".join(lines) if lines else "ledger: no entries yet"


def report(limit: int = 20) -> str:
    """Human-readable recent history + integrity (for the CLI and the MCP tool)."""
    entries = load(limit)
    return render(entries, verified=verify())


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ok, detail = verify()
    if "--verify" in argv:
        print(f"ledger integrity: {'ok' if ok else 'BROKEN'} — {detail}")
        return 0 if ok else 1
    if "--json" in argv:
        print(json.dumps({"ok": ok, "detail": detail, "entries": load(50)}, ensure_ascii=False, indent=2))
        return 0 if ok else 1
    limit = 20
    if "-n" in argv:
        try:
            limit = int(argv[argv.index("-n") + 1])
        except (IndexError, ValueError):
            pass
    print(render(load(limit), verified=(ok, detail)))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
