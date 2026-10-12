"""Permissions — a risk class per tool and a policy gate the MCP server enforces.

Everything Conductor exposes is a capability an agent can invoke. Today most are
read-only, but two are not: `keys_lookup` reaches 1Password (and, with
`reveal=true`, Touch ID + a temp file). "Read-only before mutating" is a house rule; this
is its *enforcement*, not just its aspiration.

Each tool carries a risk class — `read`, `sensitive`, `write` — and a small local policy
decides what the server will actually run. Default policy is safe-by-default: all reads
are allowed; `sensitive` actions are denied unless explicitly enabled. A known `write`
tool may *plan* freely (dry-run, `apply` falsy, which mutates nothing); only its mutating
call (`apply=true`) needs `--allow-write <tool>`. Unknown tools are treated as `write` and
denied outright, so a new tool is closed until opened.

Policy lives at `~/Library/Application Support/conductor/policy.json`.

    python -m conductor policy                  # show policy + tool classes
    python -m conductor policy --read-only      # deny everything non-read
    python -m conductor policy --standard       # default
    python -m conductor policy --allow-reveal   # permit keys.lookup reveal=true
    python -m conductor policy --allow-write <tool>  # permit a write tool's apply=true
    python -m conductor policy --deny <tool> / --allow <tool>
"""

from __future__ import annotations

import json
import sys

from conductor import config


def _policy_path():
    return config.state_file("policy.json")


# read < sensitive < write. Unknown tools default to `write` (closed until opened).
def risk_map() -> dict[str, str]:
    """Tool → risk class, derived from the MCP tool schema (single source of truth)."""
    from conductor.mcp.tools import TOOLS

    return {t["name"]: t.get("risk", "write") for t in TOOLS}


def gated_args(tool: str) -> dict:
    """The tool's declared gated arguments, from its registry entry.

    Deriving this from the registry — rather than comparing the tool name against a
    literal — is what keeps the gate attached to the *capability* instead of the string.
    A renamed tool carries its gate with it.
    """
    from conductor.mcp.tools import by_name

    return (by_name(tool) or {}).get("gatedArgs") or {}


def unknown_policy_names() -> list[str]:
    """Policy allow/deny entries that match no registered tool.

    Such an entry is a silent no-op: the rule simply stops applying and nothing says so.
    That is precisely how a tool rename drops a `deny` — so it is reported (by `render`,
    and by a gate check) rather than ignored.
    """
    known = set(risk_map())
    p = _load()
    return sorted({n for n in [*p.get("allow", []), *p.get("deny", [])] if n not in known})


_DEFAULT = {"mode": "standard", "allow_reveal": False, "allow": [], "deny": []}


def _load() -> dict:
    try:
        data = json.loads(_policy_path().read_text(encoding="utf-8"))
        return {**_DEFAULT, **data}
    except (OSError, ValueError):
        return dict(_DEFAULT)


def _save(policy: dict) -> None:
    path = _policy_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(policy, indent=2), encoding="utf-8")


def risk(tool: str) -> str:
    return risk_map().get(tool, "write")


def check(tool: str, args: dict | None = None) -> str | None:
    """Return None to allow, or a denial reason string. Never raises."""
    args = args or {}
    policy = _load()
    if tool in policy.get("deny", []):
        return f"{tool} is explicitly denied by policy"
    if tool in policy.get("allow", []):
        return None
    r = risk(tool)
    if policy.get("mode") == "read-only" and r != "read":
        return f"{tool} is {r} and policy mode is read-only"
    for arg, spec in gated_args(tool).items():
        if args.get(arg) and not policy.get(spec["flag"]):
            flag = spec["flag"].replace("_", "-")
            return f"{tool} {arg}=true is denied by default ({spec['why']}) — run `conductor policy --{flag}` to opt in"
    if r == "write":
        # A known write tool may *plan* freely (apply falsy = dry-run, no mutation); only
        # the mutating call is gated. An unknown tool is write-class with no contract, so
        # it stays closed entirely.
        if tool not in risk_map():
            return f"{tool} is write-class and not allowed by policy"
        if not args.get("apply"):
            return None
        return (
            f"{tool} apply=true is denied by default — run "
            f"`conductor policy --allow-write {tool}` to opt in (dry-run needs no opt-in)"
        )
    return None


def render() -> str:
    policy = _load()
    lines = [
        "permissions policy",
        f"  mode:         {policy['mode']}",
        f"  allow_reveal: {policy['allow_reveal']}",
        f"  allow:        {', '.join(policy['allow']) or '—'}",
        f"  deny:         {', '.join(policy['deny']) or '—'}",
    ]
    stale = unknown_policy_names()
    if stale:
        lines.append(f"  STALE:        {', '.join(stale)} — matches no tool; this rule is NOT applying")
        lines.append("                fix: `conductor policy --allow/--deny <tool>`")
    lines += ["", "tool classes:"]
    classes = risk_map()
    for tool in sorted(classes):
        lines.append(f"  [{classes[tool]:9}] {tool}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    policy = _load()

    if "--read-only" in argv:
        policy["mode"] = "read-only"
    if "--standard" in argv:
        policy["mode"] = "standard"
    if "--allow-reveal" in argv:
        policy["allow_reveal"] = True
    if "--deny-reveal" in argv:
        policy["allow_reveal"] = False
    if "--allow" in argv:
        tool = argv[argv.index("--allow") + 1]
        policy["allow"] = sorted(set(policy["allow"]) | {tool})
        policy["deny"] = [t for t in policy["deny"] if t != tool]
    if "--allow-write" in argv:
        tool = argv[argv.index("--allow-write") + 1]
        policy["allow"] = sorted(set(policy["allow"]) | {tool})
        policy["deny"] = [t for t in policy["deny"] if t != tool]
    if "--deny" in argv:
        tool = argv[argv.index("--deny") + 1]
        policy["deny"] = sorted(set(policy["deny"]) | {tool})
        policy["allow"] = [t for t in policy["allow"] if t != tool]

    if any(
        f in argv
        for f in ("--read-only", "--standard", "--allow-reveal", "--deny-reveal", "--allow", "--allow-write", "--deny")
    ):
        _save(policy)
        print("permissions: policy updated")
    print(render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
