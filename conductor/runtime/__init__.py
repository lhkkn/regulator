"""Conductor runtime module — machine + agent-stack readiness (v1 scope: `runtime/`).

Absorbs the spirit of `tools/audit.sh`: answer "is this machine ready to work?" by
reading reality (sysctl/vm_stat/pmset/ps/ports/docker + agent configs), never by
hand-maintained lists. Stdlib-only — psutil-free; figures are dashboard-grade
approximations (flagged in plans/ui-magi.md), not profiling-grade.

`system()`  — cpu/mem/disk/uptime/battery/net/top-procs/ports/docker for THIS Mac.
`agents()`  — the agentic stack: CLI tools, models, MCP wiring, personas, memory files.
"""

from conductor.runtime.probe import agents, system  # noqa: F401
