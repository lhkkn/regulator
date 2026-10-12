"""Native key-registry analysis for Conductor (strangler -> native port, Track D3).

The read surface of `Stack/models/keys` (list/audit/providers) now lives here:
the registry JSON (masked suffixes + hashes + locations — never plaintext) is read
directly and analysed in-process. `conductor.keys.api` is the only consumer that
matters; `Stack/models/keys` re-imports these functions so the CLI's write paths
(scan/rotate) stay consistent — one copy of the truth.

Stdlib-only. Defensive: a missing/unreadable registry degrades to an empty list.
"""

from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from datetime import date
from pathlib import Path

# Known LLM providers and the env vars that feed them. Canonical home (moved
# from Stack/models/keys/config.py during the D3 native port).
LLM_PROVIDERS: dict[str, list[str]] = {
    "anthropic": ["ANTHROPIC_API_KEY"],
    "openai": ["OPENAI_API_KEY"],
    "kimi": ["KIMI_API_KEY", "MOONSHOT_API_KEY"],
    "moonshot": ["MOONSHOT_API_KEY", "KIMI_API_KEY"],
    "glm": ["GLM_API_KEY"],
    "local": ["LOCAL_API_KEY", "OPENAI_API_KEY"],
}


def load_registry(path: Path) -> dict:
    """Read the registry JSON. Missing or malformed -> empty registry."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": 1, "keys": []}
    if not isinstance(data, dict) or not isinstance(data.get("keys"), list):
        return {"version": 1, "keys": []}
    return data


# ---------------------------------------------------------------- audit


def find_duplicates(registry: dict) -> list[dict]:
    """Keys shared across *different projects* (a rotation blast-radius signal).

    Sharing one key across hosts for the *same* project — dev on mac + prod on
    the production host — is the normal pattern and is deliberately not flagged. The signal
    worth surfacing is a key spanning unrelated projects, where rotating it
    means touching apps that have nothing to do with each other.
    """
    dupes = []
    for key in registry.get("keys", []):
        projects = {loc.get("project") for loc in key.get("locations", []) if loc.get("project")}
        if len(projects) > 1:
            dupes.append(key)
    return dupes


def find_flagged_locations(registry: dict) -> list[dict]:
    """Locations flagged as unexpected/suspicious."""
    flagged = []
    for key in registry.get("keys", []):
        for loc in key.get("locations", []):
            if loc.get("flagged", False):
                flagged.append({"key_id": key["id"], **loc})
    return flagged


def find_past_rotation(registry: dict) -> list[dict]:
    """Keys whose rotate_by date has passed."""
    today = date.today()
    out = []
    for key in registry.get("keys", []):
        rotate_by = key.get("rotate_by", "")
        if rotate_by:
            try:
                if date.fromisoformat(rotate_by) < today:
                    out.append(key)
            except ValueError:
                pass
    return out


def find_missing_rotation(registry: dict) -> list[dict]:
    """Keys with no rotation date set."""
    return [key for key in registry.get("keys", []) if not key.get("rotate_by", "")]


def _providers_from_env_var(variable: str) -> set[str]:
    """Map an env var name back to all LLM providers that accept it."""
    variable = variable.upper()
    return {p for p, vars_ in LLM_PROVIDERS.items() if variable in [v.upper() for v in vars_]}


def detect_code_providers(project_path: str) -> set[str]:
    """Heuristically detect which LLM providers a project's .py code references."""
    if not os.path.isdir(project_path):
        return set()
    pattern = re.compile(
        r'(?:os\.environ|os\.getenv)\s*(?:\.get)?\s*\(\s*["\']([A-Z_]+(?:API_KEY|API_TOKEN|SECRET_KEY))["\']',
        re.IGNORECASE,
    )
    providers: set[str] = set()
    skip = {".git", "node_modules", ".venv", "venv", "__pycache__"}
    for root, dirs, files in os.walk(project_path):
        dirs[:] = [d for d in dirs if d not in skip]
        for filename in files:
            if not filename.endswith(".py"):
                continue
            try:
                content = (Path(root) / filename).read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for match in pattern.finditer(content):
                providers |= _providers_from_env_var(match.group(1).upper())
    return providers


def build_provider_matrix(
    registry: dict,
    local_project_base: str = "~/Workspace/projects",
) -> list[dict]:
    """Per-project LLM-provider status matrix (present/missing/stale/unknown)."""
    keys_by_project: dict[tuple[str, str], dict[str, dict]] = defaultdict(dict)
    projects: set[tuple[str, str]] = set()
    project_basenames: dict[tuple[str, str], set[str]] = defaultdict(set)

    for key in registry.get("keys", []):
        for loc in key.get("locations", []):
            host, project = loc.get("host", ""), loc.get("project", "")
            if project and project != "(global)":
                projects.add((host, project))
                base = os.path.splitext(os.path.basename(loc.get("path", "")))[0]
                if base and base != project:
                    project_basenames[(host, project)].add(base)
        if key.get("provider") in LLM_PROVIDERS:
            for loc in key.get("locations", []):
                keys_by_project[(loc.get("host", ""), loc.get("project", ""))][key["provider"]] = key

    local_base = os.path.expanduser(local_project_base)
    if os.path.isdir(local_base):
        for project_name in os.listdir(local_base):
            if os.path.isdir(os.path.join(local_base, project_name)):
                projects.add(("mac", project_name))

    rows = []
    for host, project in sorted(projects):
        present = keys_by_project.get((host, project), {})
        code_providers: set[str] = set()
        candidates = [project]
        if host != "mac":
            candidates.extend(project_basenames.get((host, project), []))
        for candidate in candidates:
            if os.path.isdir(os.path.join(local_base, candidate)):
                code_providers = detect_code_providers(os.path.join(local_base, candidate))
                if code_providers:
                    break
        status = {}
        for provider in LLM_PROVIDERS:
            has_key = provider in present
            used = provider in code_providers
            status[provider] = (
                "present" if has_key and used else "stale" if has_key else "missing" if used else "unknown"
            )
        rows.append(
            {
                "host": host,
                "project": project,
                "status": status,
                "present": {p: k.get("id", "") for p, k in present.items()},
            }
        )
    return rows


def find_provider_mismatches(registry: dict) -> list[dict]:
    """LLM provider/key mismatches (missing or stale) across all hosts."""
    mismatches = []
    for row in build_provider_matrix(registry):
        for provider, status in row["status"].items():
            if status in ("missing", "stale"):
                mismatches.append(
                    {
                        "host": row["host"],
                        "project": row["project"],
                        "provider": provider,
                        "status": status,
                        "key_id": row["present"].get(provider, ""),
                        "remote": row["host"] != "mac",
                    }
                )
    return mismatches


# ---------------------------------------------------------------- list


_UNIT_SUFFIX = ".service"
_VAR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _unit_sets(host: str, unit: str, var: str) -> bool | None:
    """Does `unit` on `host` still set `var`? None when it cannot be determined.

    The probe is built without a single quote character, on purpose. The unit format is
    `Environment="VAR=value"`, so the natural pattern embeds `"` inside a command that is
    itself quoted and then sent through ssh — three levels of nesting, and the first
    version silently matched nothing, which read as "every location is stale". `.` matches
    the quote instead, and `test -f` distinguishes a missing file from a zero count (both
    of which make `grep -c` print 0 and exit 1).
    """
    from conductor import hostenv

    if not host or not unit or not _VAR_RE.match(var):
        return None
    path = f"/etc/systemd/system/{unit}"
    probe = f"if [ ! -f {path} ]; then echo MISSING; else grep -cE '^Environment=.{var}=' {path} || true; fi"
    try:
        r = hostenv.ssh(host, probe, timeout=20)
    except Exception:  # noqa: BLE001
        return None
    out = (r.stdout or "").strip().splitlines()
    if not out or out[-1] == "MISSING":
        return None
    try:
        return int(out[-1]) > 0
    except ValueError:
        return None


def find_stale_locations(registry: dict) -> list[dict]:
    """Registered locations whose target no longer sets the variable they claim.

    The failure this exists for: a registry that carries a key location
    for weeks after `bff8e9b` deleted the self-login. Every reader — human and agent alike —
    took it to be true, and that single stale row is why a session was needed to establish
    that the app has no such secret at all. The registry is the source of truth, so nothing
    was comparing it to reality.

    **Only systemd units are checked.** A unit either sets a variable or it does not, which
    is a readable fact. File paths in the registry are ambiguous (relative to the project?
    the workspace?), and a missing `.env` can be legitimate, so those are left alone rather
    than guessed at — a check that cries wolf is the thing this whole pass is undoing.
    """
    findings: list[dict] = []
    for entry in registry.get("keys", []):
        for loc in entry.get("locations", []):
            path = str(loc.get("path", ""))
            var = str(loc.get("variable", "")).strip()
            # `variable` is sometimes prose — "TASKS_LOGIN (the operator, PBKDF2 hash in DB)"
            if not path.endswith(_UNIT_SUFFIX) or not var or " " in var:
                continue
            if _unit_sets(str(loc.get("host", "")), path, var) is False:
                findings.append(
                    {
                        "key_id": entry.get("id", ""),
                        "host": loc.get("host", ""),
                        "path": path,
                        "variable": var,
                    }
                )
    return findings


def _fmt_table(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    return "\n".join("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) for row in rows)


def render_list(registry: dict) -> str:
    """Masked key inventory table (id/provider/suffix/label/rotate/locations/notes)."""
    keys = registry.get("keys", [])
    if not keys:
        return "Registry is empty. Run `scan` first.\n"
    rows = [["ID", "PROVIDER", "SUFFIX", "LABEL", "ROTATE BY", "LOCATIONS", "NOTES"]]
    for key in sorted(keys, key=lambda k: k.get("id", "")):
        locs = key.get("locations", [])
        flagged = sum(1 for loc in locs if loc.get("flagged"))
        loc_str = str(len(locs))
        if flagged:
            loc_str += f" ({flagged} flagged)"
        rows.append(
            [
                key.get("id", ""),
                key.get("provider", ""),
                f"...{key.get('suffix', '')}",
                key.get("label", "") or "—",
                key.get("rotate_by", "") or "—",
                loc_str,
                (key.get("notes", "") or "").split("\n")[0][:40],
            ]
        )
    return _fmt_table(rows) + "\n"


def render_audit(registry: dict) -> tuple[str, bool]:
    """Audit findings. Returns (text, had_issues)."""
    issues: list[str] = []

    flagged = find_flagged_locations(registry)
    if flagged:
        issues.append(f"\n{len(flagged)} key reference(s) found in unexpected files:")
        for loc in flagged:
            issues.append(
                f"  {loc['key_id']} in {loc['host']}/{loc['project']}/{loc['path']}:{loc.get('line', 0)} "
                f"({loc['variable']})"
            )

    duplicates = find_duplicates(registry)
    if duplicates:
        issues.append(f"\n{len(duplicates)} key(s) appear in multiple projects:")
        for key in duplicates:
            locs = ", ".join(f"{loc['host']}/{loc['project']}" for loc in key.get("locations", []))
            issues.append(f"  {key['id']} ({key.get('label') or 'no label'}): {locs}")

    past = find_past_rotation(registry)
    if past:
        issues.append(f"\n{len(past)} key(s) are past their rotation date:")
        for key in past:
            issues.append(f"  {key['id']} rotate_by={key.get('rotate_by')}")

    missing = find_missing_rotation(registry)
    if missing:
        issues.append(f"\n{len(missing)} key(s) have no rotation date set:")
        for key in missing:
            issues.append(f"  {key['id']} ({key.get('label') or 'no label'})")

    stale = [m for m in find_provider_mismatches(registry) if m["status"] == "stale"]
    if stale:
        issues.append(f"\n{len(stale)} LLM key(s) present but not referenced in project code:")
        for m in stale:
            remote_note = " [remote host; code not scanned]" if m.get("remote") else ""
            issues.append(f"  {m['host']}/{m['project']}: {m['provider']} ({m['key_id'] or 'no key id'}){remote_note}")

    # The registry claiming a location that reality disagrees with — the row that read
    # a deleted variable long after the app stopped using it.
    gone = find_stale_locations(registry)
    if gone:
        issues.append(f"\n{len(gone)} registered location(s) no longer exist:")
        for loc in gone:
            issues.append(f"  {loc['key_id']}: {loc['host']}/{loc['path']} no longer sets {loc['variable']}")

    if issues:
        return "Audit findings:\n" + "\n".join(issues) + "\n", True
    return "Audit passed: no issues found.\n", False
