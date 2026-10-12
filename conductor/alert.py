"""Ops alerts — push Conductor's brief flags to ntfy (the missing loop).

The gap this closes: `brief` already detects drift / low balance / a stale backup /
rotation debt, but only when someone runs it. A finding nobody is forced to see is a
log line. This turns those flags into a push, with de-duplication so an unchanged
problem doesn't nag on every run.

Config lives in the macOS Keychain (service `conductor-alerts`), the same pattern the
rest of Conductor uses — no plaintext on disk:
    ntfy-topic    the ntfy.sh topic to publish to (the topic name is the secret)
    ntfy-server   optional; defaults to https://ntfy.sh

    python -m conductor alert             # send only if flags are new/changed
    python -m conductor alert --force     # send regardless (test / heartbeat)
    python -m conductor alert --dry-run   # print what would be sent, send nothing
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import UTC, datetime

from conductor import config, keychain

_KC_SERVICE = "conductor-alerts"
_DEFAULT_SERVER = "https://ntfy.sh"
_REPEAT_HOURS = 24  # re-send an unchanged flag set at most once a day

# Flags matching these raise the push priority (things that cost money or lose data).
_URGENT = ("backup", "balance")


def _topic() -> str | None:
    return os.environ.get("NTFY_TOPIC") or keychain.get(_KC_SERVICE, "ntfy-topic")


def _server() -> str:
    return (os.environ.get("NTFY_SERVER") or keychain.get(_KC_SERVICE, "ntfy-server") or _DEFAULT_SERVER).rstrip("/")


def _state_path():
    return config.state_file("alerts.json")


def _load_state() -> dict:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except OSError, ValueError:
        return {}


def _save_state(flags: list[str]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"flags": flags, "sent_at": datetime.now(UTC).isoformat()}, indent=2), encoding="utf-8")


def _should_send(flags: list[str], state: dict, force: bool) -> bool:
    if force:
        return True
    if flags != state.get("flags", []):
        return True  # changed since last send (incl. flags→empty = recovery)
    if not flags:
        return False  # unchanged all-clear
    sent = state.get("sent_at")
    if not sent:
        return True
    try:
        age = (datetime.now(UTC) - datetime.fromisoformat(sent)).total_seconds() / 3600
    except ValueError:
        return True
    return age >= _REPEAT_HOURS


def push(
    title: str,
    body: str,
    urgent: bool = False,
    topic: str | None = None,
    server: str | None = None,
    tags: str | None = None,
) -> bool:
    """POST one message to the configured ntfy topic. Returns True if sent."""
    topic = topic or _topic()
    if not topic:
        return False
    req = urllib.request.Request(
        f"{server or _server()}/{topic}",
        data=body.encode("utf-8"),
        method="POST",
        headers={
            "Title": title,
            "Priority": "high" if urgent else "default",
            "Tags": tags or ("warning" if urgent else "white_check_mark"),
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()
    return True


def _post(topic: str, server: str, flags: list[str]) -> None:
    urgent = any(any(k in f.lower() for k in _URGENT) for f in flags)
    if flags:
        title = f"Conductor: {len(flags)} flag(s)"
        body = "\n".join(f"• {f}" for f in flags)
    else:
        title = "Conductor: all clear"
        body = "No flags — drift, balance, backup and keys all healthy."
    push(title, body, urgent=urgent, topic=topic, server=server)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    force = "--force" in argv
    dry = "--dry-run" in argv

    from conductor.brief import build_brief

    flags = build_brief()["flags"]

    if not _should_send(flags, _load_state(), force):
        print("alert: nothing new to send")
        return 0

    if dry:
        print(f"alert (dry-run): would send {len(flags)} flag(s): {flags}")
        return 0

    topic = _topic()
    if not topic:
        print("alert: no ntfy topic configured (Keychain service 'conductor-alerts', account 'ntfy-topic')")
        return 1

    try:
        _post(topic, _server(), flags)
    except Exception as exc:  # never let alerting failure crash the schedule
        print(f"alert: send failed: {exc}")
        return 1

    _save_state(flags)
    print(f"alert: sent {len(flags)} flag(s) to {_server()}/{topic}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
