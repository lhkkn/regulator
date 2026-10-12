"""Agent model-config lint — every agent's `model:` must resolve to a real model.

Agent definitions live in `Stack/agents/definitions/*.md` with YAML frontmatter. A typo
in `model:` (a provider that isn't configured, a model the provider doesn't expose) fails
*silently* at runtime — the agent just won't run, and the error surfaces only when someone
tries to use it. This turns that into a gate failure. Read-only, offline.

A provider is valid if it is declared in `Stack/harness/configs/opencode.jsonc` **or** is
opencode's built-in gateway (`opencode`). For a declared provider, the model must be one
of the models it lists.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from conductor.projects import stack_root

# opencode ships a built-in gateway provider that needs no `provider` block in the config.
_BUILTIN_PROVIDERS = {"opencode"}


def _strip_jsonc(text: str) -> str:
    """Remove `//` and `/* */` comments (string-aware) and trailing commas, so a
    `.jsonc` config can be read with the stdlib `json`."""
    out: list[str] = []
    in_str = False
    escaped = False
    i = 0
    while i < len(text):
        ch = text[i]
        if in_str:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < len(text) and text[i + 1] == "/":
            while i < len(text) and text[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < len(text) and text[i + 1] == "*":
            i += 2
            while i + 1 < len(text) and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def load_providers(path: Path) -> dict[str, set[str]]:
    """`{provider: {model, ...}}` from an opencode config. Empty dict on any error."""
    try:
        data = json.loads(_strip_jsonc(path.read_text(encoding="utf-8")))
    except OSError, ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    providers: dict[str, set[str]] = {}
    for name, spec in (data.get("provider") or {}).items():
        models = spec.get("models") if isinstance(spec, dict) else None
        providers[name] = set(models) if isinstance(models, dict) else set()
    return providers


def _frontmatter(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    fm: dict[str, str] = {}
    for line in text[3:end].splitlines():
        key, sep, val = line.partition(":")
        if sep:
            fm[key.strip()] = val.strip()
    return fm


def report() -> list[str]:
    """A failure message per agent whose `model:` doesn't resolve. Never raises."""
    agents_dir = stack_root() / "agents" / "definitions"
    config = stack_root() / "harness" / "configs" / "opencode.jsonc"
    if not agents_dir.is_dir() or not config.is_file():
        return []  # no stack layout (e.g. CI) — nothing to check
    providers = load_providers(config)
    valid = set(providers) | _BUILTIN_PROVIDERS

    failures: list[str] = []
    for path in sorted(agents_dir.glob("*.md")):
        model = _frontmatter(path).get("model")
        if not model:
            continue
        provider, _, name = model.partition("/")
        if not name:
            failures.append(f"{path.name}: malformed model '{model}' (expected provider/model)")
        elif provider not in valid:
            failures.append(f"{path.name}: model '{model}' — provider '{provider}' is neither configured nor built-in")
        elif provider in providers and name not in providers[provider]:
            known = ", ".join(sorted(providers[provider])) or "(none)"
            failures.append(
                f"{path.name}: model '{model}' — '{name}' is not a model of provider '{provider}' (have: {known})"
            )
    return failures
