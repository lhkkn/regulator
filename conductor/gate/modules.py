"""Module checks — each internal module must import and expose the surface the gate
(and the MCP tools) depend on: keys, tokens, runtime, brief, health, eval."""

from __future__ import annotations

from conductor.gate.runner import Check


def check_keys(c: Check) -> None:
    """Track D3/D4 — the keys module must import natively (analysis + vault).

    The read surface (list/audit) is NATIVE since the D3 port: `analysis` reads the
    registry JSON directly. The apikey_registry CLI is only the write path (scan/
    rotate) — its absence degrades, not fails. `op` (1Password CLI) is OPTIONAL:
    when present, D4 reveal is available; when absent, the masked surface still works.
    """
    try:
        from conductor.keys import analysis, api, vault
    except Exception as exc:  # import failure is a hard fail
        c.expect(False, "conductor.keys failed to import", str(exc))
        return
    c.expect(
        callable(analysis.load_registry) and callable(analysis.render_list) and callable(analysis.render_audit),
        "keys.analysis surface incomplete (load_registry/render_list/render_audit missing)",
        "define the analysis surface in conductor/keys/analysis.py",
    )
    c.expect(
        callable(api.inventory) and isinstance(api.inventory(), list),
        "keys.inventory missing or not returning a list (MAGI Keys view depends on it)",
        "define inventory() in conductor/keys/api.py reading the masked registry JSON",
    )
    c.expect(
        callable(vault.lookup) and callable(vault.op_available),
        "keys.vault surface incomplete (lookup/op_available missing)",
        "define lookup + op_available in conductor/keys/vault.py",
    )


def check_tokens(c: Check) -> None:
    """Track tokens/ — spend parsers must import and tolerate this machine's logs."""
    try:
        from conductor.tokens import spend
    except Exception as exc:
        c.expect(False, "conductor.tokens failed to import", str(exc))
        return
    c.expect(
        callable(spend.daily) and callable(spend.summary),
        "tokens surface incomplete (daily/summary missing)",
        "define daily + summary in conductor/tokens/spend.py",
    )
    try:
        rows = spend.daily(1)  # 1-day window: cheap smoke test, must never raise
        c.expect(
            isinstance(rows, list),
            "tokens.daily did not return a list",
            "make daily() defensive per plans/ui-magi.md",
        )
    except Exception as exc:
        c.expect(False, "tokens.daily raised (parsers must be defensive)", str(exc))


def check_runtime(c: Check) -> None:
    """Track runtime/ — the probe must return live machine facts."""
    try:
        from conductor.runtime import probe
    except Exception as exc:
        c.expect(False, "conductor.runtime failed to import", str(exc))
        return
    try:
        s = probe.system()
        c.expect(
            "cpu" in s and "disk" in s and "memory" in s,
            "runtime.system missing core fields",
            "system() must return cpu/memory/disk keys",
        )
    except Exception as exc:
        c.expect(False, "runtime.system raised (probes must be defensive)", str(exc))


def check_harnesses(c: Check) -> None:
    """Every harness with a versioned config must also be probed by `agents()`.

    `stack.py` discovers `harness/configs/` by glob, but `runtime.probe.agents()` is a
    hand-written list — so the two views drift, and a new harness shows up in
    `conductor stack` while staying invisible in `conductor brief`/`machine`. That is
    exactly what happened to `dsh`, and nothing said so: the drift is silent.

    The harness key is the first dash-separated token of the config's filename, which is
    how the configs are already named (`opencode.jsonc` → `opencode`,
    `dsh-tui.patch.yml` → `dsh`). This check is the only thing that makes the two agree.
    """
    try:
        from conductor.projects import stack_root
        from conductor.runtime import probe
    except Exception as exc:  # noqa: BLE001
        c.expect(False, "harness probe modules failed to import", str(exc))
        return

    cfg_dir = stack_root() / "harness" / "configs"
    if not cfg_dir.is_dir():
        return  # nothing to compare on a machine without the Stack checked out
    wanted = {p.stem.split("-")[0] for p in cfg_dir.iterdir() if p.is_file() and not p.name.startswith(".")}
    try:
        probed = set(probe.agents()) - {"mcp_tools"}
    except Exception as exc:  # noqa: BLE001
        c.expect(False, "runtime.agents() raised (probes must be defensive)", str(exc))
        return
    missing = sorted(wanted - probed)
    c.expect(
        not missing,
        f"harness(es) with a versioned config but no probe: {', '.join(missing)}",
        "add a probe in conductor/runtime/probe.py and list it in agents()",
    )


def check_brief(c: Check) -> None:
    """Track the brief/ digest — build_brief() must return a non-empty text + flags list.

    Every collector is individually defensive (a failure degrades to a section, never a
    crash), but the top-level shape must hold so the CLI and MCP tool stay usable.
    """
    try:
        from conductor.brief import build_brief
    except Exception as exc:
        c.expect(False, "conductor.brief failed to import", str(exc))
        return
    try:
        brief = build_brief(live_health=False)  # gate stays offline — no live probes
    except Exception as exc:
        c.expect(False, "build_brief raised (must be defensive)", str(exc))
        return
    c.expect(
        isinstance(brief.get("text"), str) and brief["text"].strip(),
        "build_brief did not return non-empty text",
        "ensure build_brief() composes a markdown digest even when collectors degrade",
    )
    c.expect(
        isinstance(brief.get("flags"), list),
        "build_brief did not return a flags list",
        "build_brief() must include a 'flags' list (empty if all clear)",
    )


def check_health(c: Check) -> None:
    """Live health module must import and degrade safely (probe is defensive)."""
    try:
        from conductor.health import probe, report, section
    except Exception as exc:
        c.expect(False, "conductor.health failed to import", str(exc))
        return
    try:
        data = probe(offline=True)  # never touch the network in the gate
    except Exception as exc:
        c.expect(False, "health.probe raised (must be defensive)", str(exc))
        return
    c.expect(
        isinstance(data.get("entries"), list) and isinstance(data.get("flags"), list),
        "health.probe did not return entries/flags lists",
        "fix conductor/health.py probe()",
    )
    c.expect(
        callable(report) and callable(section),
        "health surface incomplete (report/section missing)",
        "define report + section in conductor/health.py",
    )


def check_fleet(c: Check) -> None:
    """Track the run plane — fleet.toml must be readable, declared() must return a
    list, and render() must compose offline (the gate never probes the network)."""
    try:
        from conductor import fleet
    except Exception as exc:
        c.expect(False, "conductor.fleet failed to import", str(exc))
        return
    try:
        declared = fleet.declared()
        text = fleet.render(probe=False)
    except Exception as exc:
        c.expect(False, "fleet.declared/render raised (must be defensive)", str(exc))
        return
    c.expect(isinstance(declared, list), "fleet.declared did not return a list", "fix conductor/fleet/__init__.py")
    c.expect(
        isinstance(text, str) and bool(text.strip()),
        "fleet.render returned empty text",
        "compose a text block in conductor/fleet/__init__.py render()",
    )


def check_eval(c: Check) -> None:
    """Track eval — the MCP surface must meet its golden-set gate (deterministic)."""
    try:
        from conductor.eval import harness
    except Exception as exc:
        c.expect(False, "conductor.eval failed to import", str(exc))
        return
    if not harness.GOLDEN.is_file():
        c.expect(
            False,
            f"eval golden set missing: {harness.GOLDEN}",
            "create eval/golden.jsonl (one JSON-RPC case per line)",
        )
        return
    try:
        ok, msg = harness.run()
    except Exception as exc:
        c.expect(
            False,
            "eval.run raised (golden set must be parseable and cases must not raise)",
            str(exc),
        )
        return
    c.expect(ok, f"eval gate not met:\n{msg}", "fix the regression, or update the golden set deliberately")
