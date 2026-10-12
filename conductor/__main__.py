"""`python -m conductor` entry point.

A command registry, not an if-chain: each capability maps to (module, callable, arg
style). The USAGE line is generated from the same table, so it can never drift from the
actual commands.
"""

from __future__ import annotations

import importlib
import sys

from conductor import __version__

# name → (module, attribute, arg style)
#   "none" → call fn()                 (self-contained command)
#   "rest" → call fn(argv[2:])         (subcommand args)
#   "argv" → call fn(argv)             (the legacy full-argv CLI surface)
COMMANDS: dict[str, tuple[str, str, str]] = {
    "check": ("conductor.check", "main", "none"),
    "config": ("conductor.settings", "main", "rest"),
    "init": ("conductor.init", "main", "rest"),
    "brief": ("conductor.brief", "main", "rest"),
    "stack": ("conductor.stack", "main", "rest"),
    "machine": ("conductor.machine", "main", "rest"),
    "state": ("conductor.context.state", "main", "argv"),
    "projects": ("conductor.projects.cli", "main", "argv"),
    "fleet": ("conductor.fleet", "main", "rest"),
    "worktree": ("conductor.worktree", "main", "rest"),
    "memory": ("conductor.memory", "main", "rest"),
    "crp": ("conductor.crps", "main", "rest"),
    "watch": ("conductor.watch", "main", "rest"),
    "traces": ("conductor.trace", "main", "rest"),
    "policy": ("conductor.permissions", "main", "rest"),
    "local": ("conductor.local", "main", "rest"),
    "flow": ("conductor.flows", "main", "rest"),
    "ledger": ("conductor.ledger", "main", "rest"),
    "act": ("conductor.actuator", "main", "rest"),
    "backup": ("conductor.backup", "main", "none"),
    "health": ("conductor.health", "main", "rest"),
    "alert": ("conductor.alert", "main", "rest"),
    "maintenance": ("conductor.maintenance", "main", "rest"),
    "eval": ("conductor.eval.harness", "main", "rest"),
    "mcp": ("conductor.mcp.server", "main", "none"),
}

USAGE = "usage: regulator {" + "|".join(COMMANDS) + "} [opts]\n"


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv
    if len(argv) == 2 and argv[1] in ("--version", "-V"):
        print(f"regulator {__version__}")
        return 0
    if len(argv) == 2 and argv[1] in ("--help", "-h"):
        sys.stdout.write(USAGE)
        return 0
    if len(argv) < 2:
        sys.stderr.write(USAGE)
        return 2
    cmd = argv[1]
    if cmd not in COMMANDS:
        sys.stderr.write(f"unknown command: {cmd}\n{USAGE}")
        return 2
    module_name, attr, style = COMMANDS[cmd]
    fn = getattr(importlib.import_module(module_name), attr)
    if style == "none":
        return fn()
    if style == "argv":
        return fn(argv)
    return fn(argv[2:])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
