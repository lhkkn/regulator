"""Live AI-stack health for Conductor — wire the real world into the brief.

Runs `Stack/models/health/ai-stack-check.py` (read-only free probes: balance checks, key-auth
checks, one 1-token chat) as a subprocess — the strangler pattern over the tools/
script — parses its verdicts, and turns STALE / DEAD / expired-OAuth entries into
flags. Memoized in-process (600s TTL) so the brief and MCP tool stay fast.

Fully defensive: a missing script, timeout, or probe failure degrades to "health
unavailable" — never raises, never breaks the verify gate. `offline=True` skips
the subprocess entirely (for the gate, which must stay fast and offline-safe).

The point: Conductor's registry-driven checks measure *process* (drift, rotation
dates). This measures *operational reality* — are the credentials behind the stack
actually alive right now?
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

from conductor.projects import stack_root

_TTL = 600  # seconds; don't hammer the probes on every brief call
_TIMEOUT = 40
_CACHE: dict = {"ts": 0.0, "data": None}

_BAD = re.compile(r"STALE|DEAD")
_NEG_MIN = re.compile(r"(-?\d+(?:\.\d+)?)m left")


def _probe_script() -> Path:
    return stack_root() / "models" / "health" / "ai-stack-check.py"


def _parse(output: str) -> tuple[list[dict], list[str]]:
    """Parse `ai_stack_check.py` stdout into (entries, flags).

    Entry lines look like `  <id>      <status>` (two-or-more-space separated);
    header/legend lines have no such split and are skipped.
    """
    entries, flags = [], []
    for line in output.splitlines():
        parts = re.split(r"\s{2,}", line.strip())
        if len(parts) < 2 or not parts[0]:
            continue
        key_id, status = parts[0], parts[1]
        bad = bool(_BAD.search(status))
        m = _NEG_MIN.search(status)
        if m and float(m.group(1)) < 0:  # an OAuth token past its expiry
            bad = True
        entries.append({"id": key_id, "status": status, "ok": not bad})
        if bad:
            flags.append(f"[health] {key_id}: {status}")
    return entries, flags


def probe(offline: bool = False) -> dict:
    """Run the health probe (memoized). Defensive — returns a dict, never raises."""
    if offline:
        return {"ok": False, "ts": None, "entries": [], "flags": [], "source": "probe skipped (offline gate)"}
    now = time.time()
    if _CACHE["data"] and now - _CACHE["ts"] < _TTL:
        return _CACHE["data"]
    script = _probe_script()
    if not script.is_file():
        return {
            "ok": False,
            "ts": None,
            "entries": [],
            "flags": [],
            "source": f"health probe unavailable: {script} missing",
        }
    try:
        proc = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {"ok": False, "ts": None, "entries": [], "flags": [], "source": f"health probe failed: {exc}"}
    if proc.returncode != 0:
        return {"ok": False, "ts": None, "entries": [], "flags": [], "source": f"health probe exited {proc.returncode}"}
    entries, flags = _parse(proc.stdout)
    data = {
        "ok": True,
        "ts": time.strftime("%H:%M"),
        "entries": entries,
        "flags": flags,
        "source": "Stack/models/health/ai-stack-check.py",
    }
    _CACHE.update({"ts": now, "data": data})
    return data


def report(offline: bool = False) -> str:
    """Render the full health block (for the `health` MCP tool)."""
    data = probe(offline=offline)
    lines = [f"AI stack health — {data['ts'] or 'unavailable'}"]
    lines.append("=" * 20)
    if not data["ok"]:
        lines.append(f"  {data['source']}")
    for e in data["entries"]:
        lines.append(f"  {e['id']:<26} {e['status']}")
    return "\n".join(lines)


def section(offline: bool = False) -> tuple[list[str], str]:
    """Return (flags, markdown body) for the brief's AI-health section."""
    data = probe(offline=offline)
    if not data["ok"]:
        return data["flags"], f"- {data['source']}"
    body = "\n".join(f"- {e['id']}: {e['status']}" for e in data["entries"]) or "- (no probes)"
    return data["flags"], body


def main(argv: list[str] | None = None) -> int:
    """CLI: print the live AI-stack health report (free network probes)."""
    argv = list(sys.argv[1:] if argv is None else argv)
    print(report(offline="--offline" in argv))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
