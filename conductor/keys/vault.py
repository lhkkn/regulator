"""Vault retrieval for the key registry (Track D4).

`lookup` — masked by default (metadata from `registry.json`, no auth, no secret); with
`reveal=True`, fetches the plaintext value from 1Password via `op item get` and writes it to
a short-lived `0600` temp file, returning only the path (temp-file-ref: the secret never
appears in the tool-result / chat transcript).

Auth model: a **1Password service account** (read-only, scoped to the `workspace-credentials`
vault). Its token lives in macOS Keychain (`conductor-op-token`), read at call time — no
standing plaintext on disk, no interactive prompt, and it works from any process (the MCP
server included). Fails closed (clear error) if `op`, the token, or the vault link is absent;
the masked path works with zero deps.

The agent (or this code) never logs or echoes a retrieved value. Only the key id, mode, temp
path, and outcome are recorded in `conductor/logs/keys-access.jsonl`.
"""

from __future__ import annotations

import json
import os
import secrets as _secrets
import shutil
import subprocess
import time
from pathlib import Path

from conductor import keychain
from conductor.keys.api import registry_path

_LOG = Path(__file__).resolve().parents[2] / "logs" / "keys-access.jsonl"
_TEMP_DIR = Path(os.environ.get("TMPDIR", "/tmp")) / "conductor-keys"
_TEMP_TTL = 600  # seconds; stale temp files are reaped on each lookup


# Homebrew (Apple Silicon / Intel) and system locations. launchd runs the UI server
# with a minimal PATH (/usr/bin:/bin:...) that excludes Homebrew, so PATH alone misses `op`.
_OP_FALLBACKS = ("/opt/homebrew/bin/op", "/usr/local/bin/op", "/usr/bin/op")


def _op_bin() -> str | None:
    """Absolute path to the 1Password CLI: PATH first, then common install locations."""
    found = shutil.which("op")
    if found:
        return found
    for cand in _OP_FALLBACKS:
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def op_available() -> bool:
    """True if the 1Password CLI (`op`) is resolvable (PATH or a common install dir)."""
    return _op_bin() is not None


def _op_env(token: str) -> dict:
    """Env for an `op` call: the service-account token plus `OP_LOAD_DESKTOP_APP_SETTINGS=false`.

    Without the latter, `op` stat()s 1Password's app-data container
    (`~/Library/Group Containers/2BUA8C4S2C.com.1password/…/settings/`), which macOS gates
    behind the App-Data TCC prompt ("'python3.14' would like to access data from other apps").
    Service-account auth is direct, so the desktop settings load is unnecessary.
    """
    return dict(os.environ, OP_SERVICE_ACCOUNT_TOKEN=token, OP_LOAD_DESKTOP_APP_SETTINGS="false")


def service_account_token() -> str | None:
    """Read the Conductor 1Password service-account token from macOS Keychain, or None.

    Stored as a generic password with service `conductor-op-token`. Returns None if absent or
    unreadable so callers can degrade gracefully (masked path needs no token).
    """
    return keychain.get("conductor-op-token", os.environ.get("USER", "")) or None


def _op_get_args(op_ref: str) -> list[str]:
    """Build the `op item get` args (sans binary) for a vault reference. Service accounts
    require an explicit --vault (no unscoped queries) and won't accept a full op://
    reference alongside --vault — so decompose op://<vault>/<item>/<field> and add --reveal."""
    parts = op_ref.split("/")
    if len(parts) >= 5 and parts[0] == "op:":
        vault, item, field = parts[2], parts[3], parts[4]
        return ["item", "get", item, "--vault", vault, "--fields", field, "--reveal"]
    return ["item", "get", op_ref]


def reveal_value(op_ref: str) -> str | None:
    """Fetch a key's plaintext from 1Password via the service account.

    Internal, programmatic path (e.g. per-key balance probes): returns the raw value,
    or None on any failure (no op_ref, `op` absent, no token, op error, timeout). Never
    logs or prints the value. The user-facing `lookup` keeps its granular error strings.
    """
    if not op_ref:
        return None
    op_bin = _op_bin()
    if not op_bin:
        return None
    token = service_account_token()
    if not token:
        return None
    env = _op_env(token)
    try:
        proc = subprocess.run([op_bin, *_op_get_args(op_ref)], capture_output=True, text=True, timeout=60, env=env)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.rstrip("\n") or None


def _load_registry() -> dict:
    with registry_path().open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _find_key(registry: dict, key_id: str) -> dict | None:
    for key in registry.get("keys", []):
        if key.get("id") == key_id:
            return key
    return None


def _cleanup_stale_temps() -> None:
    """Delete conductor temp files older than _TEMP_TTL. Bounds secret exposure on disk."""
    if not _TEMP_DIR.is_dir():
        return
    now = time.time()
    for p in _TEMP_DIR.iterdir():
        try:
            if p.is_file() and now - p.stat().st_mtime > _TEMP_TTL:
                p.unlink()
        except OSError:
            pass


def _write_temp(key_id: str, value: str) -> Path:
    """Write `value` to a new 0600 temp file and return its path. Never prints the value."""
    _TEMP_DIR.mkdir(mode=0o700, exist_ok=True)
    name = f"conductor-key-{_safe(key_id)}-{_secrets.token_hex(4)}"
    path = _TEMP_DIR / name
    # O_CREAT | O_EXCL + 0600 via os.open avoids a race and a world-readable window.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, value.encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(path, 0o600)
    return path


def _safe(s: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in s)[:40] or "key"


def _log_access(payload: dict) -> None:
    _LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **payload}
    with _LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _masked_summary(key: dict) -> str:
    locs = key.get("locations", [])
    op_ref = key.get("op_ref", "")
    lines = [
        f"id:         {key.get('id', '')}",
        f"provider:   {key.get('provider', '')}",
        f"suffix:     ...{key.get('suffix', '')}",
        f"label:      {key.get('label', '') or '—'}",
        f"rotate_by:  {key.get('rotate_by', '') or '—'}",
        f"locations:  {len(locs)}",
        f"vault:      {'linked (op_ref set) — reveal available' if op_ref else 'NOT linked — migrate to vault before reveal'}",
    ]
    return "\n".join(lines)


def lookup(key_id: str, reveal: bool = False, mode: str = "tmpfile") -> str:
    """Look up a registered key. Masked by default; `reveal=True` fetches from 1Password.

    Returns a human-readable string. On `reveal`, the plaintext is written to a 0600 temp file
    and the returned text contains only the file path (never the secret). Failures (missing key,
    `op` absent, app locked, no vault link) return a clear error string, not an exception.
    """
    _cleanup_stale_temps()

    try:
        registry = _load_registry()
    except (OSError, ValueError) as exc:
        return f"could not read registry: {exc}"
    key = _find_key(registry, key_id)
    if key is None:
        return f"key not found: {key_id} (use keys_list to see ids)"

    if not reveal:
        _log_access({"key_id": key_id, "reveal": False, "status": "masked"})
        return _masked_summary(key) + "\n\n(pass reveal=true to fetch the value from 1Password)"

    if mode != "tmpfile":
        return f"unsupported reveal mode: {mode} (only 'tmpfile' is supported)"

    op_ref = key.get("op_ref", "")
    if not op_ref:
        _log_access({"key_id": key_id, "reveal": True, "status": "error", "error": "no op_ref"})
        return (
            f"{key_id} has no vault link (op_ref). Migrate it into the workspace-credentials "
            "vault first, then set its op_ref in registry.json."
        )
    op_bin = _op_bin()
    if not op_bin:
        _log_access({"key_id": key_id, "reveal": True, "status": "error", "error": "op missing"})
        return "1Password CLI (`op`) not found (PATH or /opt/homebrew/bin); install it to reveal secrets."
    token = service_account_token()
    if not token:
        _log_access({"key_id": key_id, "reveal": True, "status": "error", "error": "no token"})
        return (
            "no 1Password service-account token in Keychain (service `conductor-op-token`). "
            "Create a read-only service account for the workspace-credentials vault and store it."
        )

    # Service accounts require an explicit --vault (no unscoped queries) and won't accept a
    # full op:// reference alongside --vault — so decompose the reference and add --reveal to
    # return the plaintext field value. op_ref = op://<vault>/<item>/<field>.
    cmd = [op_bin, *_op_get_args(op_ref)]
    env = _op_env(token)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)
    except subprocess.TimeoutExpired:
        _log_access({"key_id": key_id, "reveal": True, "status": "error", "error": "timeout"})
        return "op item get timed out (>60s)."
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout).strip().splitlines()
        msg = err[-1] if err else f"op exited {proc.returncode}"
        _log_access({"key_id": key_id, "reveal": True, "status": "error", "error": "op_failed"})
        return (
            f"op item get failed: {msg}\n"
            "(check the op_ref is valid, the item is in the workspace-credentials vault, and "
            "the service account still has read access.)"
        )

    value = proc.stdout.rstrip("\n")
    path = _write_temp(key_id, value)
    _log_access({"key_id": key_id, "reveal": True, "mode": mode, "temp": str(path), "status": "revealed"})
    return (
        f"revealed {key_id} to a 0600 temp file (auto-deleted in ~{_TEMP_TTL // 60} min):\n"
        f"  {path}\n\n"
        "The value is NOT shown here — read the file. It never enters this transcript."
    )
