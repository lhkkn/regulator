"""Read-only key-registry helpers exposed to agents (Track D3, native).

`list_keys`   — masked inventory of registered keys (id, provider, suffix, label,
                rotate_by, locations, notes).
`audit_keys`  — registry audit findings (flagged locations, duplicates/shared keys,
                past/missing rotation, stale provider mismatches).
`inventory`   — structured masked inventory (for MAGI / brief).

NATIVE since 2026-09-17 (strangler -> native port): the read surface of
`Stack/models/keys` (list/audit/providers) moved into `conductor/keys/analysis`,
which reads the registry JSON directly — no subprocess. The CLI keeps only the write
paths (scan/rotate). The registry file holds masked suffixes + SHA-256 hashes +
locations only; plaintext never lives here. The 1Password (`op`) backend is separate
(`vault.py`).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from conductor import config
from conductor.keys import analysis
from conductor.projects import find_workspace_root


def registry_path() -> Path:
    """Resolve the apikey registry JSON path.

    `CONDUCTOR_KEYS_REGISTRY` env > `keys_registry` config > the workspace default
    `<workspace>/keys/registry.json`. Mirrors `projects.registry_path()`.
    """
    env = config.setting("keys_registry")
    if env:
        return Path(env).expanduser()
    try:
        ws = find_workspace_root()
    except FileNotFoundError:
        return config.app_dir() / "keys" / "registry.json"
    return ws / "keys" / "registry.json"


def list_keys() -> str:
    """Masked key inventory (id / provider / suffix / label / rotate_by / locations / notes)."""
    return analysis.render_list(analysis.load_registry(registry_path()))


def audit_keys() -> str:
    """Registry audit: flagged locations, shared/duplicate keys, rotation gaps, stale providers."""
    return analysis.render_audit(analysis.load_registry(registry_path()))[0]


def inventory() -> list[dict]:
    """Structured masked inventory, read straight from the registry JSON (the file
    holds only suffixes + SHA-256 hashes + locations — never plaintext, so a direct
    read keeps the masked-only contract). `rotate_days` is days until rotate_by
    (negative = overdue; None = no date)."""
    data = analysis.load_registry(registry_path())
    entries = data.get("keys", []) if isinstance(data, dict) else []
    today = date.today()
    out = []
    for k in entries:
        rot = k.get("rotate_by")
        days = None
        if rot:
            try:
                days = (date.fromisoformat(rot) - today).days
            except ValueError:
                pass
        out.append(
            {
                "id": k.get("id"),
                "provider": k.get("provider"),
                "suffix": k.get("suffix"),
                "label": k.get("label"),
                "rotate_by": rot,
                "rotate_days": days,
                "locations": k.get("locations") or [],
                "notes": k.get("notes"),
            }
        )
    return out
