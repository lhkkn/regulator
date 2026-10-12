"""macOS Keychain reads — one implementation for every module that needs a secret.

Secrets stay in the Keychain (or 1Password behind it); a module that needs one calls
`keychain.get(service, account)` and gets the value or None. Never raises.
"""

from __future__ import annotations

import subprocess


def get(service: str, account: str, *, timeout: int = 10) -> str | None:
    """Return the password for (service, account), or None on absence/failure."""
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None
