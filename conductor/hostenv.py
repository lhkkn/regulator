"""Host seam — the one module that knows the operating system.

Every OS-specific primitive Conductor needs — a non-interactive `ssh` round-trip to the
prod host, `launchctl`/`plutil` for launch agents, the macOS Keychain — lives here, so the
rest of the package is host-agnostic. Porting Conductor to another OS means reimplementing
*this file*, not hunting `subprocess` calls across modules. Primitives are defensive
(they return `''` / `None` / a synthetic result on failure) so callers never crash.

`conductor.launchagent` and `conductor.runtime.probe` are themselves host-specific by
design and keep their own `subprocess` use; `conductor.keychain` remains the Keychain
primitive that `host.keychain_get` delegates to.
"""

from __future__ import annotations

import json
import subprocess
import sys

from conductor import keychain

_SSH_OPTS = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=10")


def platform_name() -> str:
    return sys.platform


def is_macos() -> bool:
    return sys.platform == "darwin"


def keychain_get(service: str, account: str) -> str | None:
    """Read a generic password from the macOS Keychain (delegates to the keychain seam)."""
    return keychain.get(service, account)


def ssh(host: str, remote: str, *, timeout: int = 40):
    """One non-interactive ssh round-trip. On failure returns a synthetic
    `CompletedProcess` (returncode 1) rather than raising, so callers branch on
    `.returncode` exactly as they would on a real run."""
    try:
        return subprocess.run(["ssh", *_SSH_OPTS, host, remote], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(args=["ssh", host], returncode=1, stdout="", stderr=str(exc))


def launchctl_list(*, timeout: int = 15) -> str:
    """`launchctl list` output, or '' if unavailable."""
    try:
        return subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def plist_json(plist) -> dict | None:
    """A plist file as a dict via `plutil`, or None on any failure."""
    try:
        r = subprocess.run(
            ["plutil", "-convert", "json", "-o", "-", str(plist)], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    try:
        data = json.loads(r.stdout)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None
