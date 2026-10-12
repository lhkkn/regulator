"""The concrete actuator contracts — Conductor's write capabilities.

Each contract is a named, reversible-first mutation with a declared precondition, act,
verifier and rollback (the envelope lives in `conductor/actuator.py`). This module is
the registry the MCP server and flows dispatch through; every contract maps 1:1 to a
`write`-class tool in `conductor/mcp/tools.py`, and `conductor.check` enforces that.

Today (all dry-run by default; `apply=true` performs the mutation):
  backup.run                — run the off-machine restic backup, then prove it's fresh
  projects.register         — append a directory to registry.toml, then prove the docs match
  projects.register-drift   — bulk-register every unregistered project dir
  projects.archive          — flip a project to archived and move its dir to work/archive/
  docs.render               — regenerate the registry docs when stale
  keys.retire               — retire location-less keys marked revoked/orphaned
  docker.prune              — prune dangling Docker images + build cache
  worktree.add              — create an isolated worktree + branch for a parallel agent
  worktree.remove           — remove a task's worktree (the branch survives)

Both default to dry-run. `conductor.run_tool(name, {**args, "apply": True})` performs
the mutation; `apply=False` only runs the precondition and returns the plan.
"""

from __future__ import annotations

import json
import re
import subprocess
import tomllib
from pathlib import Path

from conductor import actuator, config
from conductor.actuator import Contract
from conductor.projects import find_workspace_root, locate, registry_path, stack_root


def _render(check: bool = False) -> tuple[int, str]:
    """Render the registry docs via the data-returning core (it never prints)."""
    from conductor.projects import render

    return render.run(check=check)


# --- projects.register -------------------------------------------------------


def _locate(name: str):
    """The project's directory if it exists — live trees only (never an archived one)."""
    return locate(name, include_archive=False)


def _registered(name: str) -> bool:
    try:
        with registry_path().open("rb") as fh:
            data = tomllib.load(fh)
    except OSError, tomllib.TOMLDecodeError:
        return False
    return any(p.get("name") == name for p in data.get("project", []))


def _block(name: str, args: dict) -> str:
    fields = {
        "name": name,
        "title": args.get("title") or name,
        "status": args.get("status") or "active",
        "tier": args.get("tier") or "personal",
        "deploy": args.get("deploy") or "none",
    }
    if args.get("url"):
        fields["url"] = args["url"]
    if args.get("notes"):
        fields["notes"] = args["notes"]
    return (
        "\n".join(["[[project]]"] + [f"{k} = {json.dumps(str(v), ensure_ascii=False)}" for k, v in fields.items()])
        + "\n"
    )


def _register_precondition(args: dict) -> tuple[bool, str]:
    name = str(args.get("project") or "").strip()
    if not name:
        return False, "project name required"
    if "/" in name or "\\" in name or any(ch.isspace() for ch in name):
        return False, f"invalid project name: {name!r}"
    try:
        located = _locate(name)
    except FileNotFoundError as exc:
        return False, f"workspace not found: {exc}"
    if located is None:
        return False, (f"no directory for '{name}' under work/apps, work/research, or the workspace root")
    if _registered(name):
        return False, f"'{name}' is already registered"
    return True, f"unregistered directory found at {located}"


def _register_act(args: dict) -> dict:
    name = str(args["project"]).strip()
    path = registry_path()
    prior = path.read_text(encoding="utf-8")
    text = prior if prior.endswith("\n") else prior + "\n"
    text += "\n" + _block(name, args)
    path.write_text(text, encoding="utf-8")
    code, _ = _render()
    return {"registry": str(path), "registered": name, "render_rc": code, "_prior": prior}


def _register_verify(args: dict, effect: dict) -> tuple[bool, str]:
    name = str(args["project"]).strip()
    if not _registered(name):
        return False, f"'{name}' is not in registry.toml after the write"
    code, out = _render(check=True)
    if code != 0:
        return False, f"generated docs stale after render: {out.strip()[:160]}"
    return True, f"'{name}' registered; generated docs current"


def _register_rollback(args: dict, effect: dict) -> None:
    prior = effect.get("_prior")
    if prior is None:
        raise RuntimeError("no prior registry content captured")
    registry_path().write_text(prior, encoding="utf-8")
    _render()


def _unregistered_dirs() -> list[str]:
    """Directories under work/apps + work/research that aren't in registry.toml."""
    from conductor.projects import find_workspace_root, load

    try:
        known = {p.get("name") for p in load().get("project", [])}
        root = find_workspace_root()
    except OSError, ValueError, FileNotFoundError:
        return []
    out: list[str] = []
    for base in ("work/apps", "work/research"):
        d = root / base
        if not d.is_dir():
            continue
        for child in sorted(d.iterdir()):
            if child.is_dir() and not child.name.startswith(".") and child.name not in known:
                out.append(child.name)
    return out


def _register_drift_precondition(args: dict) -> tuple[bool, str]:
    names = _unregistered_dirs()
    if not names:
        return False, "no unregistered project directories"
    return True, f"{len(names)} unregistered dir(s): {', '.join(names)}"


def _register_drift_act(args: dict) -> dict:
    names = _unregistered_dirs()
    path = registry_path()
    prior = path.read_text(encoding="utf-8")
    text = prior if prior.endswith("\n") else prior + "\n"
    for name in names:
        text += "\n" + _block(name, {})
    path.write_text(text, encoding="utf-8")
    code, _ = _render()
    return {
        "registry": str(path),
        "registered": names,
        "render_rc": code,
        "summary": f"registered {len(names)} project(s): {', '.join(names)}",
        "_prior": prior,
    }


def _register_drift_verify(args: dict, effect: dict) -> tuple[bool, str]:
    still = [n for n in (effect.get("registered") or []) if n in _unregistered_dirs()]
    if still:
        return False, f"still unregistered after the write: {', '.join(still)}"
    code, out = _render(check=True)
    if code != 0:
        return False, f"generated docs stale after render: {out.strip()[:160]}"
    return True, f"{len(effect.get('registered') or [])} project(s) registered; docs current"


# --- projects.archive --------------------------------------------------------


def _set_project_status(text: str, name: str, status: str) -> str:
    """Rewrite one `[[project]]` block's `status = "…"` line, preserving everything
    else (the registry is hand-edited — comments and formatting must survive)."""
    out: list[str] = []
    in_block = False
    target = False
    found = False
    for ln in text.splitlines():
        stripped = ln.strip()
        if stripped == "[[project]]":
            in_block, target = True, False
            out.append(ln)
            continue
        if stripped.startswith("[") and stripped.endswith("]"):  # [meta] / [[launch]] …
            in_block = False
            out.append(ln)
            continue
        if in_block:
            m = re.match(r'\s*name\s*=\s*"([^"]*)"', ln)
            if m:
                target = m.group(1) == name
            if target and re.match(r'\s*status\s*=\s*"', ln):
                ln = re.sub(r'(status\s*=\s*")[^"]*(")', rf"\g<1>{status}\g<2>", ln)
                target, found = False, True
        out.append(ln)
    if not found:
        raise ValueError(f"no status line for project '{name}'")
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def _project_status(name: str) -> str | None:
    try:
        data = tomllib.loads(registry_path().read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None
    for p in data.get("project", []):
        if p.get("name") == name:
            return p.get("status")
    return None


def _archive_precondition(args: dict) -> tuple[bool, str]:
    name = str(args.get("project") or "").strip()
    if not name or "/" in name or "\\" in name or any(ch.isspace() for ch in name):
        return False, f"invalid project name: {name!r}"
    status = _project_status(name)
    if status is None:
        return False, f"'{name}' is not registered"
    src = _locate(name)  # live trees only (include_archive=False)
    dst = find_workspace_root() / "work" / "archive" / name
    if dst.exists():
        return False, f"archive target already exists: {dst}"
    if status == "archived":
        # A half-done archive: the status was flipped (by hand, or by a run that died
        # between the move and the write) but the directory is still in the live tree.
        # Refusing here left no repair path through the actuator — which is the one
        # place the rollback and the ledger live. Completing it is the same operation.
        if src is None:
            return False, f"'{name}' is already archived and has no live directory"
        return True, f"'{name}' is archived but still on disk — completing the move to work/archive/{name}"
    if src is None:
        return False, f"no live directory for '{name}' to move"
    return True, f"'{name}' ({status}) -> work/archive/{name}"


def _archive_act(args: dict) -> dict:
    name = str(args["project"]).strip()
    src = _locate(name)
    dst = find_workspace_root() / "work" / "archive" / name
    path = registry_path()
    prior = path.read_text(encoding="utf-8")
    moved: dict = {}
    if src is not None and not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
        moved = {"src": str(src), "dst": str(dst)}
    path.write_text(_set_project_status(prior, name, "archived"), encoding="utf-8")
    code, _ = _render()
    return {
        "registry": str(path),
        "archived": name,
        "moved_to": str(dst),
        "render_rc": code,
        "summary": f"archived {name} (moved to work/archive/{name})",
        "_prior": prior,
        "_moved": moved,
    }


def _archive_verify(args: dict, effect: dict) -> tuple[bool, str]:
    name = str(args["project"]).strip()
    if _project_status(name) != "archived":
        return False, f"'{name}' status is not 'archived' after the write"
    dst = Path(effect.get("moved_to") or "")
    if not dst.is_dir():
        return False, f"archived directory missing: {dst}"
    if _locate(name) is not None:
        return False, f"'{name}' still has a live directory after the write"
    code, out = _render(check=True)
    if code != 0:
        return False, f"generated docs stale after render: {out.strip()[:160]}"
    return True, f"'{name}' archived under work/archive/; docs current"


def _archive_rollback(args: dict, effect: dict) -> None:
    moved = effect.get("_moved") or {}
    src, dst = Path(moved.get("src", "")), Path(moved.get("dst", ""))
    if dst.is_dir() and src and not src.exists():
        src.parent.mkdir(parents=True, exist_ok=True)
        dst.rename(src)
    prior = effect.get("_prior")
    if prior is None:
        raise RuntimeError("no prior registry content captured")
    registry_path().write_text(prior, encoding="utf-8")
    _render()


# --- backup.run --------------------------------------------------------------


def _backup_precondition(args: dict) -> tuple[bool, str]:
    from conductor import backup

    if args.get("force"):
        return True, "force requested — running the backup regardless of freshness"
    rec = backup.latest()  # memoized probe — a dry-run must stay cheap
    if not rec.get("ok"):
        return False, f"cannot read backup state: {rec.get('error')}"
    age = rec.get("age_hours")
    stale = config.BACKUP_STALE_HOURS
    if age is not None and age < stale:
        return False, (f"backup already fresh ({age}h < {stale}h) — nothing to do (pass force=true to run anyway)")
    return True, f"latest snapshot {age}h ago (>= {stale}h) — the backup is due"


def _backup_act(args: dict) -> dict:
    script = stack_root() / "ops" / "backup" / "backup.sh"
    if not script.is_file():
        raise FileNotFoundError(f"backup script not found: {script}")
    timeout = int(args.get("timeout") or 3600)
    proc = subprocess.run(["bash", str(script)], capture_output=True, text=True, timeout=timeout)
    return {
        "script": str(script),
        "returncode": proc.returncode,
        "stdout_tail": (proc.stdout or "").strip()[-400:],
        "stderr_tail": (proc.stderr or "").strip()[-400:],
    }


def _backup_verify(args: dict, effect: dict) -> tuple[bool, str]:
    if effect.get("returncode") != 0:
        return False, (f"backup.sh exited {effect.get('returncode')}: {(effect.get('stderr_tail') or '')[:160]}")
    from conductor import backup

    rec = backup.latest(fresh=True)
    if not rec.get("ok"):
        return False, f"snapshot unreadable after the run: {rec.get('error')}"
    age = rec.get("age_hours")
    return ((age is not None and age < config.BACKUP_STALE_HOURS), f"latest snapshot {age}h ago")


# --- docs.render -------------------------------------------------------------


def _docs_precondition(args: dict) -> tuple[bool, str]:
    from conductor.projects import render

    code, text = render.run(check=True)
    return (code != 0), text


def _docs_act(args: dict) -> dict:
    from conductor.projects import render

    prior = {str(p): (p.read_text(encoding="utf-8") if p.exists() else None) for p in render.targets()}
    code, text = render.run()
    return {"render_rc": code, "summary": text, "_prior": prior}


def _docs_verify(args: dict, effect: dict) -> tuple[bool, str]:
    from conductor.projects import render

    code, text = render.run(check=True)
    return (code == 0), text


def _docs_rollback(args: dict, effect: dict) -> None:
    for path_str, content in (effect.get("_prior") or {}).items():
        p = Path(path_str)
        if content is None:
            p.unlink(missing_ok=True)
        else:
            p.write_text(content, encoding="utf-8")


# --- keys.retire -------------------------------------------------------------

_DEAD_WORDS = ("REVOKED", "ORPHAN", "ROTATED OUT", "RETIRED")


def _dead_keys() -> list[dict]:
    from conductor.keys.api import registry_path as keys_registry

    try:
        data = json.loads(keys_registry().read_text(encoding="utf-8"))
    except OSError, ValueError:
        return []
    return [
        k
        for k in data.get("keys", [])
        if not k.get("locations") and any(w in (k.get("notes") or "").upper() for w in _DEAD_WORDS)
    ]


def _retire_precondition(args: dict) -> tuple[bool, str]:
    dead = _dead_keys()
    if not dead:
        return False, "no dead (location-less, revoked/orphaned) keys"
    ids = sorted(k.get("id", "?") for k in dead)
    return True, f"{len(ids)} dead key(s): {', '.join(ids)}"


def _retire_act(args: dict) -> dict:
    from conductor.keys.api import registry_path as keys_registry

    path = keys_registry()
    prior = path.read_text(encoding="utf-8")
    data = json.loads(prior)
    retired = sorted(k.get("id", "?") for k in _dead_keys())
    data["keys"] = [k for k in data["keys"] if k.get("id") not in set(retired)]
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return {
        "retired": retired,
        "summary": f"retired {len(retired)} dead key(s): {', '.join(retired)}",
        "_prior": prior,
    }


def _retire_verify(args: dict, effect: dict) -> tuple[bool, str]:
    dead = _dead_keys()
    if dead:
        return False, f"{len(dead)} dead key(s) still present"
    return True, "no dead keys remain"


def _retire_rollback(args: dict, effect: dict) -> None:
    from conductor.keys.api import registry_path as keys_registry

    prior = effect.get("_prior")
    if prior is None:
        raise RuntimeError("no prior keys registry captured")
    keys_registry().write_text(prior, encoding="utf-8")


# --- docker.prune ------------------------------------------------------------

_RECLAIM = re.compile(r"(?:Total reclaimed space|Total):\s*([0-9.]+\s*[KMGT]?B)")


def _docker_precondition(args: dict) -> tuple[bool, str]:
    from conductor import proc

    if not proc.find("docker", "/opt/homebrew/bin/docker", "/usr/local/bin/docker"):
        return False, "docker not installed"
    return True, "docker present"


def _docker_act(args: dict) -> dict:
    reclaimed: list[str] = []
    codes: list[int] = []
    for argv in (["docker", "image", "prune", "-f"], ["docker", "builder", "prune", "-f"]):
        try:
            out = subprocess.run(argv, capture_output=True, text=True, timeout=90)
            codes.append(out.returncode)
        except Exception:  # noqa: BLE001 — a failed prune is reported via the code
            codes.append(1)
            continue
        m = _RECLAIM.search(out.stdout or "")
        if m and m.group(1).strip() not in ("0B", "0 B"):
            reclaimed.append(m.group(1).strip())
    return {
        "returncodes": codes,
        "reclaimed": reclaimed,
        "summary": f"pruned Docker ({', '.join(reclaimed)})" if reclaimed else "",
    }


def _docker_verify(args: dict, effect: dict) -> tuple[bool, str]:
    codes = effect.get("returncodes") or []
    if not codes or any(c != 0 for c in codes):
        return False, f"prune command(s) failed: {codes}"
    return True, "prune completed"


# --- worktree.add / worktree.remove -----------------------------------------


def _worktree_args(args: dict) -> tuple[str, str, str]:
    from conductor import worktree

    name = str(args.get("project") or "").strip()
    task = str(args.get("task") or "").strip()
    agent = str(args.get("agent") or "agent").strip()
    if not (name and task and agent) or not worktree._valid(name, task, agent):
        raise ValueError(f"invalid project/task/agent: {name!r}/{task!r}/{agent!r}")
    d = locate(name, include_archive=False)
    if d is None:
        raise FileNotFoundError(f"no live directory for '{name}'")
    if not worktree.is_repo(d):
        raise NotADirectoryError(f"'{name}' is not a git repository")
    return name, task, agent


def _worktree_add_precondition(args: dict) -> tuple[bool, str]:
    from conductor import worktree

    try:
        name, task, agent = _worktree_args(args)
    except (ValueError, FileNotFoundError, NotADirectoryError) as exc:
        return False, str(exc)
    path = worktree.target(name, task)
    if path is not None and path.exists():
        return False, f"worktree path already exists: {path}"
    d = locate(name, include_archive=False)
    branch = f"{agent}/{task}"
    ref = subprocess.run(
        ["git", "-C", str(d), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        capture_output=True,
        text=True,
        env=worktree._git_env(),
    )
    if ref.returncode == 0:
        return False, f"branch already exists: {branch}"
    return True, f"would create {path} on branch {branch}"


def _worktree_add_act(args: dict) -> dict:
    from conductor import worktree

    name, task, agent = _worktree_args(args)
    path = worktree.add(name, task, agent)
    return {
        "project": name,
        "task": task,
        "branch": f"{agent}/{task}",
        "path": str(path),
        "summary": f"created worktree {path} (branch {agent}/{task})",
    }


def _worktree_add_verify(args: dict, effect: dict) -> tuple[bool, str]:
    from conductor import worktree

    path = Path(effect.get("path") or "")
    if not path.is_dir():
        return False, f"worktree directory missing: {path}"
    paths = {w["path"] for w in worktree.list_for(effect.get("project", ""))}
    if str(path) not in paths:
        return False, f"{path} not registered with git after the add"
    return True, f"{path} exists and is a git worktree"


def _worktree_add_rollback(args: dict, effect: dict) -> None:
    from conductor import worktree

    name, task = effect.get("project", ""), effect.get("task", "")
    branch = effect.get("branch", "")
    worktree.remove(name, task, force=True)
    d = locate(name, include_archive=False)
    if d is not None and branch:
        subprocess.run(
            ["git", "-C", str(d), "branch", "-D", branch],
            capture_output=True,
            text=True,
            env=worktree._git_env(),
        )


def _worktree_remove_precondition(args: dict) -> tuple[bool, str]:
    from conductor import worktree

    try:
        name, task, _ = _worktree_args(args)
    except (ValueError, FileNotFoundError, NotADirectoryError) as exc:
        return False, str(exc)
    path = worktree.target(name, task)
    if path is None or not path.is_dir():
        return False, f"no worktree directory to remove: {path}"
    if str(path) not in {w["path"] for w in worktree.list_for(name)}:
        return False, f"{path} is not a registered git worktree"
    return True, f"would remove {path} (the branch is kept)"


def _worktree_remove_act(args: dict) -> dict:
    from conductor import worktree

    name, task, _ = _worktree_args(args)
    path = worktree.remove(name, task, force=bool(args.get("force")))
    return {"project": name, "task": task, "path": str(path), "summary": f"removed worktree {path}"}


def _worktree_remove_verify(args: dict, effect: dict) -> tuple[bool, str]:
    from conductor import worktree

    path = str(effect.get("path") or "")
    if Path(path).is_dir() and path in {w["path"] for w in worktree.list_for(effect.get("project", ""))}:
        return False, f"{path} still present after the remove"
    return True, f"{path} removed (branch kept)"


# --- registry ----------------------------------------------------------------

_BACKUP = Contract(
    name="backup.run",
    tool="backup_run",
    description="run the off-machine restic backup and verify the snapshot is fresh",
    precondition=_backup_precondition,
    act=_backup_act,
    verify=_backup_verify,
    reversible=False,
    rollback_hint="idempotent — a re-run creates a new snapshot; no undo needed",
)

_REGISTER = Contract(
    name="projects.register",
    tool="projects_register",
    description="append an existing project dir to registry.toml and re-render the docs",
    precondition=_register_precondition,
    act=_register_act,
    verify=_register_verify,
    rollback=_register_rollback,
    reversible=True,
    rollback_hint="restore the prior registry.toml (byte-for-byte) and re-render",
)

_REGISTER_DRIFT = Contract(
    name="projects.register-drift",
    tool="projects_register_drift",
    description="register every unregistered project dir under work/apps + work/research",
    precondition=_register_drift_precondition,
    act=_register_drift_act,
    verify=_register_drift_verify,
    rollback=_register_rollback,
    reversible=True,
    rollback_hint="restore the prior registry.toml (byte-for-byte) and re-render",
)

_ARCHIVE = Contract(
    name="projects.archive",
    tool="projects_archive",
    description="archive a project: flip its status to archived and move its dir to work/archive/",
    precondition=_archive_precondition,
    act=_archive_act,
    verify=_archive_verify,
    rollback=_archive_rollback,
    reversible=True,
    rollback_hint="restore the prior registry.toml and move the directory back",
)

_DOCS = Contract(
    name="docs.render",
    tool="docs_render",
    description="regenerate the registry docs (REGISTRY.md + README) when they are stale",
    precondition=_docs_precondition,
    act=_docs_act,
    verify=_docs_verify,
    rollback=_docs_rollback,
    reversible=True,
    rollback_hint="restore the prior generated docs",
)

_RETIRE = Contract(
    name="keys.retire",
    tool="keys_retire",
    description="retire location-less keys whose notes mark them revoked/orphaned",
    precondition=_retire_precondition,
    act=_retire_act,
    verify=_retire_verify,
    rollback=_retire_rollback,
    reversible=True,
    rollback_hint="restore the prior keys registry (byte-for-byte)",
)

_DOCKER = Contract(
    name="docker.prune",
    tool="docker_prune",
    description="prune dangling Docker images + build cache",
    precondition=_docker_precondition,
    act=_docker_act,
    verify=_docker_verify,
    reversible=False,
    rollback_hint="prune is destructive; dangling objects cannot be restored",
)

_WORKTREE_ADD = Contract(
    name="worktree.add",
    tool="worktree_add",
    description="create an isolated git worktree + branch for a parallel agent task",
    precondition=_worktree_add_precondition,
    act=_worktree_add_act,
    verify=_worktree_add_verify,
    rollback=_worktree_add_rollback,
    reversible=True,
    rollback_hint="remove the worktree and delete the task branch",
)

_WORKTREE_REMOVE = Contract(
    name="worktree.remove",
    tool="worktree_remove",
    description="remove a task's worktree (the branch survives)",
    precondition=_worktree_remove_precondition,
    act=_worktree_remove_act,
    verify=_worktree_remove_verify,
    reversible=False,
    rollback_hint="untracked files in the tree are lost; the commits live on the branch — re-add to restore",
)

# --- crp.file ----------------------------------------------------------------


def _crp_file_precondition(args: dict) -> tuple[bool, str]:
    """A CRP must be bounded and not a duplicate of one already awaiting an answer.

    Delegates to `crps.check` — the same rule the CLI enforces. The chore agent reaches
    CRPs through the CLI, so a guard that existed only here would bound the MCP path and
    leave the unattended one open.
    """
    from conductor import crps

    return crps.check(
        str(args.get("question", "")),
        actor=actuator.actor(args.get("actor")),
        default_after=str(args.get("default_after", "")).strip(),
    )


def _crp_file_act(args: dict) -> dict:
    from conductor import crps

    cid = crps.file(
        str(args.get("question", "")),
        options=args.get("options") or [],
        context=str(args.get("context", "")),
        blast_radius=str(args.get("blast_radius", "")),
        default_action=str(args.get("default_action", "")),
        default_after=str(args.get("default_after", "")),
        actor=actuator.actor(args.get("actor")),
        project=str(args.get("project", "")),
    )
    return {"crp_id": cid}


def _crp_file_verify(args: dict, effect: dict) -> tuple[bool, str]:
    """Prove the question is actually *queued*, not merely written."""
    from conductor import crps

    row = crps.get(int(effect.get("crp_id") or 0))
    if row is None:
        return False, "the CRP row is not readable"
    if row["status"] != crps.OPEN:
        return False, f"CRP #{row['id']} landed as {row['status']}, expected open"
    return True, f"CRP #{row['id']} is queued and open for an answer"


def _crp_file_rollback(args: dict, effect: dict) -> None:
    from conductor import crps

    crps.withdraw(int(effect.get("crp_id") or 0))


_CRP_FILE = Contract(
    name="crp.file",
    tool="crp_file",
    description="file a Consultation Request Pack — a durable question for the human",
    precondition=_crp_file_precondition,
    act=_crp_file_act,
    verify=_crp_file_verify,
    rollback=_crp_file_rollback,
    reversible=True,
    rollback_hint="withdraw the CRP (status → withdrawn)",
)

CONTRACTS: dict[str, Contract] = {
    _BACKUP.name: _BACKUP,
    _REGISTER.name: _REGISTER,
    _REGISTER_DRIFT.name: _REGISTER_DRIFT,
    _ARCHIVE.name: _ARCHIVE,
    _DOCS.name: _DOCS,
    _RETIRE.name: _RETIRE,
    _DOCKER.name: _DOCKER,
    _WORKTREE_ADD.name: _WORKTREE_ADD,
    _WORKTREE_REMOVE.name: _WORKTREE_REMOVE,
    _CRP_FILE.name: _CRP_FILE,
}
_BY_TOOL: dict[str, Contract] = {c.tool: c for c in CONTRACTS.values()}


def get(name_or_tool: str) -> Contract | None:
    """Look a contract up by its name (`backup.run`) or its tool (`backup_run`)."""
    return CONTRACTS.get(name_or_tool) or _BY_TOOL.get(name_or_tool)


def list_contracts() -> list[Contract]:
    return list(CONTRACTS.values())


def execute(name_or_tool: str, args: dict | None = None) -> dict:
    """Run a contract and return the raw actuator result (outcome, precondition, effect…).

    Callers that need to distinguish "refused — not due" from "failed" use this; the MCP
    tool path uses `run_tool`.
    """
    contract = get(name_or_tool)
    args = dict(args or {})
    if contract is None:
        return {
            "contract": name_or_tool,
            "outcome": "unknown",
            "ok": False,
            "apply": bool(args.get("apply")),
            "precondition": {"ok": False, "detail": "unknown contract"},
        }
    return actuator.run(contract, args, apply=bool(args.get("apply", False)))


def run_tool(name_or_tool: str, args: dict | None = None) -> tuple[str, bool]:
    """Run a contract by name/tool. Returns (rendered_text, is_error). Dry-run is the
    default; `apply=True` in args performs the mutation."""
    result = execute(name_or_tool, args)
    if result.get("outcome") == "unknown":
        return f"unknown contract: {name_or_tool}", True
    apply = bool((args or {}).get("apply", False))
    return actuator.render(result), bool(apply and not result.get("ok"))


def render_list() -> str:
    lines = ["contracts — write capabilities (dry-run by default; --apply to run):"]
    for c in CONTRACTS.values():
        undo = c.rollback_hint or ("restores prior state" if c.reversible else "irreversible")
        lines.append(f"  {c.name:18} {c.tool:30} {'reversible  ' if c.reversible else 'IRREVERSIBLE'}")
        lines.append(f"      {c.description}")
        lines.append(f"      undo: {undo}")
    return "\n".join(lines)
