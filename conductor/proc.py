"""Process / binary helpers shared across modules."""

from __future__ import annotations

import os
import shutil


def find(name: str, *fallbacks: str) -> str | None:
    """Locate `name` on PATH, else the first executable among `fallbacks`.

    launchd runs with a minimal PATH that excludes Homebrew, so a PATH-only lookup
    misses tools like restic; the fallbacks cover the common install locations.
    """
    found = shutil.which(name)
    if found:
        return found
    for cand in fallbacks:
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None
