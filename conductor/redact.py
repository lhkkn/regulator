"""Redaction — the one place that masks secret-looking fields and caps long strings.

Both the tool-call trace and the action ledger must never persist a secret. This is the
shared policy: any mapping key whose *name* hints at a secret is replaced with `***`
(recursively through nested dicts/lists), and — when `max_str` is set — long strings are
truncated so an oversized payload cannot bloat the log. It never raises: a value it
cannot understand is returned as-is.

    from conductor.redact import redact
    redact(args)                    # mask secret-keyed values only
    redact(effect, max_str=500)     # mask + cap string length
"""

from __future__ import annotations

SECRET_HINTS = ("token", "secret", "password", "passwd", "otp", "pin", "credential")


def _is_secret(key) -> bool:
    return any(h in str(key).lower() for h in SECRET_HINTS)


def redact(value, *, max_str: int | None = None):
    """Recursively mask secret-keyed values; optionally cap string length. Never raises."""
    if isinstance(value, dict):
        return {k: ("***" if _is_secret(k) else redact(v, max_str=max_str)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v, max_str=max_str) for v in value]
    if max_str is not None and isinstance(value, str) and len(value) > max_str:
        return value[:max_str] + "…"
    return value
