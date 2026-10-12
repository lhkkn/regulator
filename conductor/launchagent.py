"""Generate + install macOS LaunchAgents — one plist renderer, not a copy per job.

Scheduled jobs (the watcher, the ollama server, alerts, maintenance, the MCP bridge) all
need the same plist shape. This is the single source: `render()` builds the XML,
`install()`/`uninstall()` manage the file + launchctl. Paths come from `config`, so the
target dir is test-overridable.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from conductor import config

_HEADER = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
    '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
    '<plist version="1.0">\n<dict>\n'
)
_FOOTER = "</dict>\n</plist>\n"


def path(label: str) -> Path:
    return config.launch_agents_dir() / f"{label}.plist"


def render(
    label: str,
    program: list[str],
    *,
    workdir: str | None = None,
    interval: int | None = None,
    run_at_load: bool = False,
    keep_alive: bool = False,
    log: str | None = None,
) -> str:
    """Build a plist: a label, a program argv, and optional scheduling/output keys."""
    lines = [f"    <key>Label</key>\n    <string>{label}</string>", "    <key>ProgramArguments</key>\n    <array>"]
    lines += [f"        <string>{arg}</string>" for arg in program]
    lines.append("    </array>")
    if workdir:
        lines += ["    <key>WorkingDirectory</key>", f"    <string>{workdir}</string>"]
    if interval:
        lines += ["    <key>StartInterval</key>", f"    <integer>{int(interval)}</integer>"]
    if run_at_load:
        lines += ["    <key>RunAtLoad</key>", "    <true/>"]
    if keep_alive:
        lines += ["    <key>KeepAlive</key>", "    <true/>"]
    if log:
        lines += [
            "    <key>StandardOutPath</key>",
            f"    <string>{log}</string>",
            "    <key>StandardErrorPath</key>",
            f"    <string>{log}</string>",
        ]
    return _HEADER + "\n".join(lines) + "\n" + _FOOTER


def install(label: str, text: str, *, log: str | None = None) -> str:
    """Write the plist and (re)load it. Returns the plist path."""
    if log:
        Path(log).parent.mkdir(parents=True, exist_ok=True)
    target = path(label)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    subprocess.run(["launchctl", "unload", str(target)], capture_output=True)
    subprocess.run(["launchctl", "load", str(target)], capture_output=True)
    return str(target)


def uninstall(label: str) -> bool:
    """Unload and remove the plist. Returns whether a plist existed."""
    target = path(label)
    subprocess.run(["launchctl", "unload", str(target)], capture_output=True)
    existed = target.exists()
    target.unlink(missing_ok=True)
    return existed
