"""Per-key provider balances — read-only (v1: DeepSeek `/user/balance`).

The spend chart (`spend.py`) only sees calls that flow through a local agent CLI log.
App-side calls (apps hitting provider APIs directly, Open WebUI) never appear there — so a provider
account balance, read per key, is the only signal that a background service is draining
a shared account (the "$20 vanished" failure mode). This module closes that gap: for
every registered key whose provider exposes a balance endpoint, it reveals the key from
1Password (read-only service account) and queries the provider.

Record shape:
    {id, provider, label, suffix, ok, balance, currency, is_available, error}
      ok            True when the provider returned a parseable balance
      balance       float (total balance) or None
      currency      e.g. "USD" or None
      is_available  provider's own spend flag (can you call?) or None
      error         short reason when ok is False (else None)

Defensive: a missing registry, absent `op`/token, or a failing provider degrades to
fewer/error records — never raises. Memoized (balances move slowly).

Registry-driven and dynamic: `all_balances` re-reads the key registry on every probe,
so a newly registered key of a **supported provider** (`_PROVIDERS`) appears
automatically — within the memo TTL, or immediately on `fresh=True` (the UI's
"refresh balances" button). Adding a new provider is one entry in `_PROVIDERS`:
a balance URL + a parser `(body) -> (balance, currency, is_available)`.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from conductor.keys.api import registry_path
from conductor.keys.vault import reveal_value

_TTL = 300  # seconds; balances move slowly — one pass per UI warm cycle is plenty
_MEMO: tuple[float, list[dict]] | None = None

_BALANCE_URL = "https://api.deepseek.com/user/balance"


def _parse_deepseek(body: dict) -> tuple[float | None, str | None, bool | None]:
    """DeepSeek: {"is_available": bool, "balance_infos": [{"total_balance": "19.77",
    "currency": "USD", ...}]}. Total balance is the string we care about."""
    infos = body.get("balance_infos") or []
    available = body.get("is_available")
    if not isinstance(infos, list) or not infos:
        return None, None, available
    info = infos[0] if isinstance(infos[0], dict) else {}
    raw = info.get("total_balance")
    try:
        balance = float(raw) if raw is not None else None
    except (TypeError, ValueError):
        balance = None
    return balance, info.get("currency"), available


# provider -> (balance URL, response parser). Add providers here as they're adopted.
_PROVIDERS: dict[str, tuple[str, Callable[[dict], tuple]]] = {
    "deepseek": (_BALANCE_URL, _parse_deepseek),
}


def _query(url: str, key: str, parser: Callable[[dict], tuple]) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error": f"HTTP {exc.code}"}
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        return {"ok": False, "error": str(exc)[:80]}
    balance, currency, available = parser(body)
    return {
        "ok": balance is not None,
        "balance": balance,
        "currency": currency,
        "is_available": available,
        "error": None,
    }


def _probe(key: dict, url: str, parser: Callable[[dict], tuple]) -> dict:
    """Reveal one key and query its provider. Returns a per-key record (never raises)."""
    rec = {
        "id": key.get("id"),
        "provider": key.get("provider", ""),
        "label": key.get("label") or "",
        "suffix": key.get("suffix") or "",
        "ok": False,
        "balance": None,
        "currency": None,
        "is_available": None,
        "error": None,
    }
    value = reveal_value(key.get("op_ref", ""))
    if not value:
        rec["error"] = "no vault value"
    else:
        rec.update(_query(url, value, parser))
    return rec


def all_balances(fresh: bool = False) -> list[dict]:
    """Per-key balances for every registered key whose provider is supported. Read-only.
    Probes run in parallel (each reveals from 1Password + hits the provider) so the
    digest stays fast. Memoized `_TTL` seconds; pass fresh=True to bypass the cache."""
    global _MEMO
    now = time.time()
    if not fresh and _MEMO is not None and now - _MEMO[0] < _TTL:
        return _MEMO[1]

    try:
        registry = json.loads(registry_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        registry = {"keys": []}

    candidates: list[tuple[dict, str, Callable]] = []
    for key in registry.get("keys", []):
        entry = _PROVIDERS.get(key.get("provider", ""))
        if not entry or not key.get("op_ref"):
            continue  # unsupported provider, or not queryable (retired / never vault-linked)
        candidates.append((key, entry[0], entry[1]))

    if candidates:
        with ThreadPoolExecutor(max_workers=8) as pool:
            out = list(pool.map(lambda c: _probe(*c), candidates))
    else:
        out = []

    out.sort(key=lambda r: (r["provider"], r["id"] or ""))
    _MEMO = (now, out)
    return out


def summary() -> dict:
    """Rollup for the Pulse KPI tiles.

    Provider balances are account-level, so keys on one account report the same figure.
    Dedupe identical (provider, currency, balance) triples as a single account so the
    total isn't multiplied by the key count (5 keys on one $19.75 account = $19.75).
    """
    recs = all_balances()
    ok = [r for r in recs if r["ok"] and r["balance"] is not None]
    accounts: dict[tuple, float] = {}
    for r in ok:
        accounts[(r["provider"], r["currency"], r["balance"])] = r["balance"]
    by_provider: dict[str, float] = {}
    for (provider, _cur, _bal), bal in accounts.items():
        by_provider[provider] = round(by_provider.get(provider, 0.0) + bal, 4)
    return {
        "total_balance": round(sum(accounts.values()), 4),
        "accounts": len(accounts),
        "by_provider": by_provider,
        "count": len(recs),
        "errors": sum(1 for r in recs if not r["ok"]),
    }
