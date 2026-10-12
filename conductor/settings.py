"""The settings schema — the one place that says what an operator can plug in.

A value like `production_host` or `backup_repo` is machine-specific: it cannot ship in
the package, but the *slot* can. This module is the checklist. Each `Slot` names a
setting, what it unlocks, and an example. Three surfaces read it, so they never drift:

  * `conductor config`  — the inventory: every slot, its value, and what it enables.
  * `conductor init`    — the scaffolder: writes config.toml from the same list.
  * `conductor brief`   — reports unset optional slots as neutral flags (via `unset()`).

Resolution order for every slot is `CONDUCTOR_<KEY>` env > `config.toml` > default
(`conductor.config.setting`). A slot with an empty resolved value and no default is
*unset* — an unavailable integration, never a pointer at somebody else's machine.

    python -m conductor config          # show every slot and its state
    python -m conductor config --unset  # only the slots still empty
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from conductor import config, projects


@dataclass(frozen=True)
class Slot:
    key: str
    kind: str  # str | path | list | table
    enables: str
    example: str = ""
    default: str = ""  # literal, or a template using {workspace}/{app}
    discoverable: bool = False
    advanced: bool = False


SLOTS: tuple[Slot, ...] = (
    Slot(
        "workspace_root",
        "path",
        "project discovery, drift checks, the registry file location",
        "~/Workspace",
        default="{home}/Workspace",
        discoverable=True,
    ),
    Slot(
        "registry_file",
        "path",
        "the project inventory (registry.toml)",
        "{workspace}/registry/registry.toml",
        default="{workspace}/registry/registry.toml",
    ),
    Slot(
        "fleet_file",
        "path",
        "the declared-service inventory (fleet.toml)",
        "{workspace}/fleet.toml",
        default="{workspace}/fleet.toml",
    ),
    Slot(
        "keys_registry",
        "path",
        "the masked API-key inventory",
        "{workspace}/keys/registry.json",
        default="{workspace}/keys/registry.json",
    ),
    Slot(
        "production_host",
        "str",
        "remote fleet probes, production drift, server backup, cron jobs",
        "my-vps (an ssh alias)",
        advanced=False,
    ),
    Slot(
        "backup_repo",
        "str",
        "off-machine backup freshness (restic repository URL)",
        "s3:https://s3.us-east-005.backblazeb2.com/mybucket/path",
    ),
    Slot(
        "backup_region",
        "str",
        "the S3 region for the restic backend (Backblaze B2 uses us-east-005)",
        "us-east-005",
        default="us-east-005",
        advanced=True,
    ),
    Slot(
        "server_backup_remote",
        "str",
        "the server DB snapshot check",
        "myremote:mybucket/sqlite",
    ),
    Slot("clef_account", "str", "the tier-2 Clef decision judge", "your Cloudflare account id", advanced=True),
    Slot(
        "clef_key_id", "str", "the tier-2 Clef decision judge", "the key-registry id of the Clef token", advanced=True
    ),
    Slot(
        "launchagent_prefixes",
        "str",
        "which macOS LaunchAgents belong to the workspace",
        "com.myapp.",
        advanced=True,
    ),
    Slot(
        "intentional_live_archive",
        "str",
        "projects allowed to keep an archive inside the live docs tree",
        "proj-a,proj-b",
        advanced=True,
    ),
    Slot(
        "known_ports",
        "list",
        "the local service probe (extra ports to watch)",
        '[{port=8100, label="my app"}]',
        advanced=True,
    ),
    Slot(
        "owner_aliases",
        "table",
        "job-to-project attribution when a name is not in the command",
        '[owner_aliases]\\ndesk="my-project"',
        advanced=True,
    ),
)

_BY_KEY = {s.key: s for s in SLOTS}


def _expand(tpl: str) -> str:
    """Expand a slot default. `{workspace}` stays literal when no workspace is found, so
    the display shows `{workspace}/registry/registry.toml` instead of a bogus `/registry/...`."""
    ws = projects.workspace_root()
    out = tpl.replace("{home}", str(Path.home())).replace("{app}", str(config.app_dir()))
    if ws:
        out = out.replace("{workspace}", str(ws))
    return out


def resolved(slot: Slot) -> tuple[str, str]:
    """(value, source) where source is 'env' | 'config' | 'default' | 'unset'."""
    if os.environ.get(f"CONDUCTOR_{slot.key.upper()}"):
        return config.setting(slot.key), "env"
    stored = config.value(slot.key, "")
    if stored:
        return str(stored), "config"
    if slot.default:
        return _expand(slot.default), "default"
    return "", "unset"


def unset() -> list[Slot]:
    """Optional slots with no value and no default — integrations that stay off."""
    return [s for s in SLOTS if not s.advanced and resolved(s)[1] == "unset"]


def _render(only_unset: bool) -> int:
    ws = projects.workspace_root()
    print(f"regulator config — workspace: {ws or '(not found)'}  ·  data: {config.app_dir()}")
    for s in SLOTS:
        value, source = resolved(s)
        if only_unset and source != "unset":
            continue
        shown = value or "(unset)"
        tag = "" if source == "unset" else f"  [{source}]"
        print(f"  {s.key:<24} {shown}{tag}")
        if source == "unset":
            print(f"  {'':<24} → enables {s.enables}")
            if s.example:
                print(f"  {'':<24}   e.g. {s.example}")
    if only_unset:
        return 0
    missing = unset()
    print()
    if missing:
        print(f"{len(missing)} optional integration(s) unconfigured: " + ", ".join(s.key for s in missing))
        print(f"Set them in {config.app_dir() / 'config.toml'} (or `regulator init`).")
    else:
        print("All core integrations configured.")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    return _render("--unset" in argv)
