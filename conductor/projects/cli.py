"""`python3 -m conductor projects {list|reconcile|render}` — Tracks A1-cont + A2.

`list`      — fast inventory snapshot, read straight from registry.toml (metadata only).
`reconcile` — reality check; runs the NATIVE engine (ported from registry/reconcile.py,
              Track A2.1) — no subprocess. --local for offline, --ops for the discovered
              operational surface.
`render`    — regenerate registry/REGISTRY.md + README.md (Track A2.2); --check for CI.

`format_inventory` and `run_reconcile` are shared with the MCP server so the CLI and
agents see identical output.
"""

from __future__ import annotations

import sys

from conductor.projects import group_by_status

USAGE = "usage: python3 -m conductor projects {list|reconcile|render} [--local|--ops|--check]\n"

_STATUS_ORDER = ["active", "maintenance", "dormant", "archived", "vendor", "empty"]


def format_inventory(status_filter: str | None = None) -> str:
    """Render the project inventory as text. Shared by the CLI and the MCP tool."""
    groups = group_by_status()
    order = [status_filter] if status_filter else _STATUS_ORDER
    body = []
    shown = 0
    for status in order:
        ps = groups.get(status, [])
        if not ps:
            continue
        body.append(f"## {status} ({len(ps)})")
        for p in sorted(ps, key=lambda x: x["name"]):
            tail = ""
            if p.get("deploy"):
                tail += f" [{p['deploy']}]"
            if p.get("url"):
                tail += f" {p['url']}"
            body.append(f"- {p['name']}{tail}")
        body.append("")
        shown += len(ps)
    header = "# Workspace project inventory"
    if status_filter:
        header += f" — {status_filter}"
    header += f" ({shown} projects)"
    return header + "\n\n" + "\n".join(body)


def run_reconcile(extra: list[str]) -> tuple[int, str]:
    """Run the native reconcile engine with output captured. Returns (exit_code, text).

    Captured (not streamed) so the MCP server can return it as a tool result without
    leaking to its own stdout — which must stay reserved for JSON-RPC.
    """
    from conductor.projects import reconcile

    return reconcile.run(local="--local" in extra, ops_flag="--ops" in extra)


def cmd_list() -> int:
    print(format_inventory())
    return 0


def cmd_reconcile(extra: list[str]) -> int:
    from conductor.projects import reconcile

    return reconcile.main(["reconcile", *extra])


def cmd_render(extra: list[str]) -> int:
    from conductor.projects import render

    return render.main(extra)


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        sys.stderr.write(USAGE)
        return 2
    sub = argv[2]
    rest = argv[3:]
    if sub == "list":
        return cmd_list()
    if sub == "reconcile":
        return cmd_reconcile(rest)
    if sub == "render":
        return cmd_render(rest)
    sys.stderr.write(f"unknown projects subcommand: {sub}\n{USAGE}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(["", "projects"]))
