"""Judge — a bounded YES/NO decision, from a decision model or the local lane.

The tier-2 eval harness needs exactly one thing: given a rubric and a model output,
decide YES or NO. Two backends answer it, preferred first:

* **Clef** (Cloudflare Workers AI) — a *decision model*. It reads a state and a schema
  of typed questions and returns a probability per option, with no free-form text to
  parse. `ask()` is the general entry point, reusable outside the harness (routing,
  triage, classification).
* **Ollama** — the local lane (`conductor/local.py`). Free text plus a tolerant YES/NO
  parse. Used when Clef is not configured or not reachable, so the judge still works
  offline.

Clef is **opt-in**: paid Workers AI calls are off unless `CONDUCTOR_CLEF_ENABLED` is set
(1/true/yes/on). The default is zero spend — the judge uses ollama, and absent a local
model it skips. Set the flag only when you want Clef.

Auth: `$CONDUCTOR_CLEF_TOKEN` or `$CLOUDFLARE_API_TOKEN`, else the registered key
`cloudflare_37ee55dd` fetched from 1Password through the conductor service account. No
token, no permission, or a transport error degrades to the ollama path — an auth failure
disables Clef for the rest of the process so it is not retried per case.

    python -m conductor eval --tier 2        # judge the tier-2 golden set
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from conductor import config, local


class ClefError(Exception):
    """A Clef call could not produce an answer."""


class NoJudge(Exception):
    """No judge backend produced a verdict."""


# --- token ------------------------------------------------------------------

_token_cache: str | None = None
_token_loaded = False
_clef_disabled = False


def _disable_clef() -> None:
    """Stop trying Clef for this process (an auth/permission failure is not transient)."""
    global _clef_disabled
    _clef_disabled = True


def reset() -> None:
    """Clear the token + disable caches (tests, and after a credential change)."""
    global _token_cache, _token_loaded, _clef_disabled
    _token_cache, _token_loaded, _clef_disabled = None, False, False


def _vault_token() -> str | None:
    """The Cloudflare token from 1Password via the key registry, or None."""
    try:
        from conductor.keys import vault
        from conductor.keys.analysis import load_registry
        from conductor.keys.api import registry_path
    except Exception:  # noqa: BLE001
        return None
    key_id = config.clef_key_id()
    if not key_id:
        return None
    registry = load_registry(registry_path())
    key = next((k for k in registry.get("keys", []) if k.get("id") == key_id), None)
    if not key:
        return None
    return vault.reveal_value(key.get("op_ref", "")) or None


def _token() -> str | None:
    global _token_cache, _token_loaded
    if not _token_loaded:
        tok = os.environ.get("CONDUCTOR_CLEF_TOKEN") or os.environ.get("CLOUDFLARE_API_TOKEN")
        if not tok:
            tok = _vault_token()
        _token_cache = (tok or "").strip() or None
        _token_loaded = True
    return _token_cache


# --- Clef -------------------------------------------------------------------


def _post(url: str, token: str, body: bytes, timeout: int) -> dict:
    """POST to Workers AI and return the parsed JSON. Raises ClefError on any failure."""
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        if exc.code in (401, 403):
            _disable_clef()
        raise ClefError(f"HTTP {exc.code}: {detail}") from exc
    except Exception as exc:  # noqa: BLE001
        raise ClefError(f"{type(exc).__name__}: {exc}") from exc


def _answers(data: dict) -> dict:
    """Extract the `answers` mapping from either response shape (`{answers}` or `{result:{answers}}`)."""
    result = data.get("result")
    if isinstance(result, dict) and isinstance(result.get("answers"), dict):
        return result["answers"]
    if isinstance(data.get("answers"), dict):
        return data["answers"]
    raise ClefError(f"no answers in Clef response: {str(data)[:160]}")


def _prob(answer: Any) -> float:
    """The probability of true from a `noul` answer (a float, or a dict with one)."""
    if isinstance(answer, bool):
        return 1.0 if answer else 0.0
    if isinstance(answer, (int, float)):
        return float(answer)
    if isinstance(answer, dict):
        # A Workers AI `noul` answer is `{"type": "noul", "noul": 0.99}`; accept the
        # API-compatible aliases too (a bare probability may arrive under any of these).
        for key in ("noul", "probability", "confidence", "prob", "p", "true"):
            val = answer.get(key)
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                return float(val)
    raise ClefError(f"unrecognized Clef answer: {answer!r}")


_ENABLED_VALUES = {"1", "true", "yes", "on"}


def enabled() -> bool:
    """Clef is opt-in: paid Workers AI calls are off unless CONDUCTOR_CLEF_ENABLED is set."""
    return os.environ.get("CONDUCTOR_CLEF_ENABLED", "").strip().lower() in _ENABLED_VALUES


def clef_available() -> bool:
    """True only when Clef is opted in and a Cloudflare token is resolvable."""
    if not enabled() or _clef_disabled:
        return False
    return _token() is not None


def ask(
    state: Any,
    questions: dict[str, Any],
    *,
    model: str | None = None,
    timeout: int | None = None,
) -> dict:
    """Run Clef and return its `answers` mapping. Raises ClefError on any failure.

    `questions` is the Clef schema (1–64 entries): `{id: {"type": "noul"|"choice"|"score",
    "instructions": str, "criteria": ...}}`. This is the reusable decision primitive —
    the judge is just one caller. Opt-in: raises ClefError when `CONDUCTOR_CLEF_ENABLED`
    is not set, so a call can never spend by accident.
    """
    if not enabled():
        raise ClefError("Clef is disabled — set CONDUCTOR_CLEF_ENABLED=1 to allow paid Workers AI calls")
    if not config.clef_account() and not os.environ.get("CONDUCTOR_CLEF_URL"):
        raise ClefError("Clef is not configured — set clef_account in config.toml")
    token = _token()
    if not token:
        raise ClefError(
            "no Cloudflare token (set CONDUCTOR_CLEF_TOKEN or CLOUDFLARE_API_TOKEN, "
            "or set clef_key_id in config.toml and link it in the key registry)"
        )
    payload = {"model": model or config.CLEF_MODEL, "state": state, "questions": questions}
    data = _post(config.clef_url(), token, json.dumps(payload).encode("utf-8"), timeout or config.CLEF_TIMEOUT)
    if data.get("success") is False:
        errors = data.get("errors") or []
        msg = errors[0].get("message") if errors and isinstance(errors[0], dict) else "request failed"
        raise ClefError(str(msg))
    return _answers(data)


# --- ollama fallback --------------------------------------------------------


def parse_yes_no(resp: str) -> bool:
    """Read a judge verdict tolerantly: prefer an explicit verdict line, else the gist."""
    text = (resp or "").strip().upper()
    for line in reversed(text.splitlines()):
        line = line.strip()
        if "VERDICT: YES" in line or line.startswith("YES"):
            return True
        if "VERDICT: NO" in line or line.startswith("NO"):
            return False
    return "YES" in text and "NO" not in text


def _prompt(rubric: str, subject: str) -> str:
    return (
        "Judge an AI output against a requirement. For each condition the requirement "
        "states, write PRESENT or MISSING on its own line. Then, on the final line, write "
        "'VERDICT: YES' if every condition is PRESENT, otherwise 'VERDICT: NO'.\n\n"
        f"REQUIREMENT: {rubric}\n\nOUTPUT:\n{subject[:8000]}\n"
    )


def ollama_available() -> bool:
    return local.server_up()


def _ollama_yes_no(rubric: str, subject: str) -> bool:
    return parse_yes_no(local.generate(_prompt(rubric, subject)))


# --- the judge --------------------------------------------------------------


def available() -> bool:
    """True when at least one judge backend can be attempted."""
    return clef_available() or ollama_available()


def verdict(rubric: str, subject: str) -> bool:
    """Decide a rubric over a subject. Clef first, then Ollama. Raises NoJudge if neither works."""
    errors: list[str] = []
    if clef_available():
        try:
            answers = ask(subject[: config.CLEF_MAX_STATE], {"satisfied": {"type": "noul", "instructions": rubric}})
            if "satisfied" not in answers:
                raise ClefError(f"no answer for 'satisfied' (got {', '.join(answers) or 'none'})")
            return _prob(answers["satisfied"]) >= 0.5
        except ClefError as exc:
            errors.append(f"clef: {exc}")
    if ollama_available():
        try:
            return _ollama_yes_no(rubric, subject)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"ollama: {type(exc).__name__}: {exc}")
    raise NoJudge("; ".join(errors) or "no judge backend available")
