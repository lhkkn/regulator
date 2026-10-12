"""Central configuration — the one place for paths, thresholds and endpoints.

Everything here is environment-overridable so tests can redirect the whole system into a
temp dir (`CONDUCTOR_APP_DIR` / `CONDUCTOR_LAUNCH_AGENTS` / `CONDUCTOR_LOGS`) and operators
can retune without touching code. Paths are *functions* (not import-time constants) so a
redirect set after import still applies; scalar thresholds are module constants read from
the environment at import.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

APP = "conductor"


def _env(name: str, default: str) -> str:
    return os.environ.get(name) or default


def app_dir() -> Path:
    """Per-user conductor data (`~/Library/Application Support/conductor` by default)."""
    return Path(_env("CONDUCTOR_APP_DIR", str(Path.home() / "Library" / "Application Support" / APP)))


def _config_toml() -> dict:
    """The operator's `config.toml` in the app dir ({} when absent or unreadable)."""
    try:
        with (app_dir() / "config.toml").open("rb") as fh:
            return tomllib.load(fh)
    except OSError, tomllib.TOMLDecodeError:
        return {}


def setting(name: str, default: str = "") -> str:
    """A machine-specific string: `CONDUCTOR_<NAME>` env > config.toml > default.

    Defaults are empty on purpose — an unset integration is *unavailable*, not
    pointed at somebody else's machine.
    """
    return os.environ.get(f"CONDUCTOR_{name.upper()}") or str(_config_toml().get(name, "") or "") or default


def value(name: str, default):
    """A structured setting (table or list) from config.toml — env cannot express these."""
    return _config_toml().get(name, default)


def production_host() -> str:
    """The remote production host (ssh alias), or "" when this install is local-only."""
    return setting("production_host")


def backup_repo() -> str:
    """The restic repository URL for off-machine backup, or "" when not configured."""
    return setting("backup_repo")


# --- paths (functions: read the environment at call time) -------------------


def launch_agents_dir() -> Path:
    return Path(_env("CONDUCTOR_LAUNCH_AGENTS", str(Path.home() / "Library" / "LaunchAgents")))


def logs_dir() -> Path:
    return Path(_env("CONDUCTOR_LOGS", str(Path.home() / "Library" / "Logs")))


def state_file(name: str) -> Path:
    """A state file under the app dir (traces, watchers, alerts, policy…)."""
    return app_dir() / name


# --- endpoints --------------------------------------------------------------

OLLAMA_URL = _env("CONDUCTOR_LOCAL_URL", "http://localhost:11434")
JUDGE_MODEL = _env("CONDUCTOR_JUDGE_MODEL", "qwen2.5:3b")

# off-machine backup (restic). The repository URL comes from `backup_repo()` —
# config.toml or CONDUCTOR_BACKUP_REPO. Empty means backup is not configured and
# every backup tool reports unavailable instead of probing a hardcoded remote.


# --- thresholds (env-overridable) -------------------------------------------


def _float(name: str, default: str) -> float:
    try:
        return float(_env(name, default))
    except ValueError:
        return float(default)


def _int(name: str, default: str) -> int:
    try:
        return int(_env(name, default))
    except ValueError:
        return int(default)


SPEND_FLAG_USD = _float("CONDUCTOR_SPEND_FLAG_USD", "2.0")
BALANCE_FLAG_USD = _float("CONDUCTOR_BALANCE_FLAG_USD", "5.0")
BACKUP_STALE_HOURS = _int("CONDUCTOR_BACKUP_STALE_HOURS", "36")
BACKUP_MEMO_SECONDS = _int("CONDUCTOR_BACKUP_MEMO_SECONDS", "300")
BACKUP_PROBE_TIMEOUT = _int("CONDUCTOR_BACKUP_PROBE_TIMEOUT", "30")
DRILL_MAX_DAYS = _int("CONDUCTOR_DRILL_MAX_DAYS", "60")
WATCH_INTERVAL_SECONDS = _int("CONDUCTOR_WATCH_INTERVAL_SECONDS", "3600")
WATCH_STALE_HOURS = _int("CONDUCTOR_WATCH_STALE_HOURS", "3")
# Consecutive failing runs after which a check is *chronic* rather than news. The watcher
# runs hourly, so 24 means "you have had a day to act on this". Past that, a finding stops
# being a warning and becomes part of the landscape: still reported, but not shouted,
# because a signal that is permanently red is one the reader learns to skip past.
CHRONIC_AFTER_RUNS = _int("CONDUCTOR_CHRONIC_AFTER_RUNS", "24")


# --- Cloudflare Clef (decision model, Workers AI) ---------------------------
#
# Clef answers a schema of typed questions and returns a probability per option
# (no free-form text). Used by the tier-2 eval judge (`conductor/judge.py`).
# The account id and the key-registry id of the token are machine-specific: they
# come from config.toml (`clef_account`, `clef_key_id`) or the env. Empty means
# Clef is not configured and the tier-2 judge reports unavailable.

CLEF_MODEL = _env("CONDUCTOR_CLEF_MODEL", "clef")
CLEF_TIMEOUT = _int("CONDUCTOR_CLEF_TIMEOUT", "30")
CLEF_MAX_STATE = _int("CONDUCTOR_CLEF_MAX_STATE", "16000")


def clef_account() -> str:
    return setting("clef_account")


def clef_key_id() -> str:
    """The key-registry id holding the Clef token (Workers AI permission)."""
    return setting("clef_key_id")


def clef_url() -> str:
    """Workers AI run endpoint for the configured Clef model (env-overridable)."""
    return _env(
        "CONDUCTOR_CLEF_URL",
        f"https://api.cloudflare.com/client/v4/accounts/{clef_account()}/ai/run/@cf/cloudflare/{CLEF_MODEL}",
    )
