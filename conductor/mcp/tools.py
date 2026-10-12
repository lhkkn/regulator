"""The MCP tool surface — schema and risk class in one place (single source of truth).

`server.py` serves these; `permissions.py` derives its policy map from them. Each tool
declares a `risk`: `read` (safe), `sensitive` (reaches a secret store / confirmation),
or `write` (mutates). Keeping the risk here — next to the schema — means a new tool can
never silently appear unclassified in the permission layer.
"""

from __future__ import annotations

from conductor.mcp import handlers

TOOLS: list[dict] = [
    {
        "name": "projects_list",
        "handler": handlers.projects_list,
        "risk": "read",
        "description": (
            "List all workspace projects from the registry (registry.toml), grouped by "
            "status (active/maintenance/dormant/archived). Fast metadata snapshot — no "
            "network, no git. Optional `status` filters to one group."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": "optional: filter to one status (active/maintenance/dormant/archived)",
                }
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "projects_reconcile",
        "handler": handlers.projects_reconcile,
        "risk": "read",
        "description": (
            "Check the workspace registry against reality: uncommitted work, branch "
            "drift, unregistered project dirs. Returns drift findings + exit code. "
            "local=true (default) is offline/fast; local=false also probes the production host "
            "services and public URLs (slower, needs ssh)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "local": {"type": "boolean", "default": True},
                "ops": {
                    "type": "boolean",
                    "default": False,
                    "description": "include the discovered operational surface (crons/services/launchagents)",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "worktrees_list",
        "handler": handlers.worktrees_list,
        "risk": "read",
        "description": (
            "List the live git worktrees per project — the parallel agents (one agent per "
            "working tree). Discovered from `git worktree list`, never hand-maintained. "
            "Reports each linked worktree with its branch, and flags the real risk: a dirty "
            "shared checkout while a worktree is active (two writers over the same files). "
            "Optional `project` scopes to one project. Read-only. See "
            "work/notes/PARALLEL-AGENTS.md."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "optional: scope to one project name"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "context_build",
        "handler": handlers.context_build,
        "risk": "read",
        "description": (
            "Assemble curated project context (CONTEXT.md + .context/ pieces, optional "
            "source files) for an LLM session — the 'context over prompts' convention. "
            "Read-only. Default returns the structured context (~tokens, no source); pass "
            "`files` globs to embed source. Pass `since` to scope to a git diff (a ref "
            "like a tag/branch/sha, 'auto' for the last tag else HEAD~1, or N for the "
            "last N commits) — embeds the changed files in full plus a changes summary "
            "(use this for refactor audits of recent pushes/deploys). Wraps "
            "Stack/harness/context/context.py."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "project name (dir under projects/)"},
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "glob patterns of source files to embed (e.g. ['app/**/*.py'])",
                },
                "exclude": {"type": "array", "items": {"type": "string"}, "description": "glob patterns to exclude"},
                "since": {
                    "type": "string",
                    "description": "scope to recent changes: git ref (tag/branch/sha), 'auto' (last tag else HEAD~1), or N (last N commits)",
                },
            },
            "required": ["project"],
            "additionalProperties": False,
        },
    },
    {
        "name": "context_state",
        "handler": handlers.context_state,
        "risk": "read",
        "description": (
            "Return a project's living state: its .context/state.md (curated sections + "
            "generated recent commits/open issues), or a freshly rendered block if absent. "
            "Read-only — never writes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "project name"},
            },
            "required": ["project"],
            "additionalProperties": False,
        },
    },
    {
        "name": "keys_list",
        "handler": handlers.keys_list,
        "risk": "read",
        "description": (
            "List registered API keys (masked): id, provider, suffix, label, rotate-by date, "
            "locations, notes. Reads the apikey registry (masked suffixes + SHA-256 hashes; "
            "never plaintext). Read-only. Use to answer 'which key does a project use / what's "
            "where' without a human copy-paste."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "keys_audit",
        "handler": handlers.keys_audit,
        "risk": "read",
        "description": (
            "Audit the key registry for problems: keys shared across projects/hosts (duplicate "
            "detection), keys past or missing their rotation date, keys in unexpected files "
            "(flagged locations), and stale LLM-provider mismatches. Read-only, masked. The "
            "agent-native version of `apikey_registry audit`."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "keys_lookup",
        "handler": handlers.keys_lookup,
        "risk": "sensitive",
        # Arguments denied unless the policy opts in: arg name → the policy flag that opens
        # it and why it is gated. Declared here, beside the schema, so the gate is
        # *derived* from the registry rather than compared against a name literal —
        # renaming this tool can no longer silently open the reveal path, and the reason
        # travels with the rule instead of living in the permission layer's source
        # (see plans/mcp-deepseek.md, Phase B).
        "gatedArgs": {
            "reveal": {"flag": "allow_reveal", "why": "reaches 1Password and triggers Touch ID"},
        },
        "description": (
            "Look up a registered key by id. MASKED by default (id/provider/suffix/label/"
            "rotate_by/vault-link) — no auth, no secret seen. Pass reveal=true to fetch the "
            "plaintext from 1Password via `op` (triggers Touch ID via the desktop app — you "
            "must be present); the value is written to a 0600 temp file and only the PATH is "
            "returned, so the secret never appears in this transcript. Use the masked call for "
            "'which key does X use'; use reveal only when you truly need the raw value."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "key_id": {"type": "string", "description": "key id from keys_list"},
                "reveal": {
                    "type": "boolean",
                    "default": False,
                    "description": "fetch the plaintext from 1Password via a read-only service account (token in Keychain); value written to a 0600 temp file, only the path returned",
                },
                "mode": {
                    "type": "string",
                    "default": "tmpfile",
                    "description": "reveal mode; only 'tmpfile' is supported",
                },
            },
            "required": ["key_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "keys_balances",
        "handler": handlers.keys_balances,
        "risk": "read",
        "description": (
            "Per-key provider balances (read-only): for every registered key whose provider "
            "exposes a balance endpoint, reveals the key from 1Password via the read-only "
            "service account and queries the provider. Returns id / label / suffix / balance / "
            "status per key. Balance is account-level (keys on one account share it). This is "
            "the drain signal the spend log can't see — app-side calls ("
            "Open WebUI) never hit an agent log. Memoized ~5 min."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "backup",
        "handler": handlers.backup,
        "risk": "read",
        "description": (
            "Off-machine backup freshness (read-only): age of the latest restic snapshot "
            "to Backblaze B2 (credentials from the macOS Keychain). The daily job is "
            "`ops/backup/backup.sh`; this is the 'has it silently stopped running?' "
            "signal. Returns ok / last snapshot time / age_hours. Memoized ~5 min."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "brief",
        "handler": handlers.brief,
        "risk": "read",
        "description": (
            "A read-only workspace digest — the reborn heartbeat: today's token spend "
            "(kimi/claude/opencode), workspace drift (reconcile), agent-stack readiness, "
            "key-rotation status, and a flags list of the highest-signal items. No args. "
            "Use for a quick 'what's the state of everything right now' rollup an agent "
            "can summarize or act on."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "health",
        "handler": handlers.health,
        "risk": "read",
        "description": (
            "Live AI-stack health from Stack/models/health/ai-stack-check.py (read-only free probes): "
            "per-credential verdicts (OK / STALE / DEAD / balance / OAuth expiry). "
            "Memoized ~10 min. Unlike brief's registry-driven key count, this "
            "is operational reality — are the credentials behind the stack alive right now?"
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "stack",
        "handler": handlers.stack,
        "risk": "read",
        "description": (
            "A text map of the workspace stack — the machine (stack/: models, harness, "
            "agents, control), the outputs (work/: apps, research, notes…), and plumbing "
            "(ops/), annotated with counts and the registry by tier. Structural + offline. "
            "Use to orient in the workspace layout."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "machine",
        "handler": handlers.machine,
        "risk": "read",
        "description": (
            "A machine-level view above the workspace: home dirs, git repos anywhere "
            "under ~, local LaunchAgents, running containers, backup age, key count. "
            "Use to reason about the whole computer, not just ~/Workspace. Read-only, bounded."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "watch",
        "handler": handlers.watch,
        "risk": "read",
        "description": (
            "Status of the proactive watcher — the always-on loop behind the stack: its "
            "heartbeat (is it actually running?) and the last result of each local check "
            "(off-machine backup, restore drill, key rotation, disk). Read-only; the loop "
            "itself runs on a LaunchAgent. Use to answer 'is anything quietly broken / is "
            "my monitoring alive?'."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "trace",
        "handler": handlers.trace,
        "risk": "read",
        "description": (
            "Recent MCP tool calls — the observability log: tool, redacted args, ok, "
            "duration, output size, newest last. Read-only, local; secrets are never "
            "recorded. Use to answer 'what did the agent actually call?' and to replay "
            "a surprising call (`conductor traces --replay <id>`)."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "permissions",
        "handler": handlers.permissions,
        "risk": "read",
        "description": (
            "The permission policy the MCP server enforces, plus each tool's risk class "
            "(read / sensitive / write). Read-only. Use to answer 'what is this stack "
            "allowed to do, and why was a call denied?'. Policy is local; default is "
            "safe-by-default — reads allowed, sensitive actions denied until opted in."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "local",
        "handler": handlers.local,
        "risk": "read",
        "description": (
            "The local-models lane (privacy/offline): the local runtime (ollama), whether "
            "its server is up at localhost:11434, which models are pulled, and whether the "
            "tier-2 eval judge is ready. Read-only. Use to check the offline lane before "
            "relying on local inference or `conductor eval --tier 2`."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "clef_ask",
        "handler": handlers.clef_ask,
        "risk": "sensitive",
        "description": (
            "Ask Cloudflare Clef — a decision model on Workers AI — a bounded set of typed "
            "questions about a state, and get calibrated probabilities back (no free-form "
            "text). Clef answers `noul` (true/false), `choice`, and `score` questions. This "
            "sends `state` off-machine to Cloudflare and uses a token with Workers AI "
            "permission (from the key registry / 1Password); it is classed `sensitive`, so "
            "read-only policy mode denies it. Opt-in: paid calls are OFF unless "
            "CONDUCTOR_CLEF_ENABLED=1 is set, so this never spends by accident. `questions` "
            "is a map of id → {type, instructions, criteria?} (1–64 entries). Optional "
            "`model`: 'clef' (default, precise) or 'clef-flash' (faster)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "state": {
                    "type": ["string", "object", "array"],
                    "description": "the content to decide on: text, or structured JSON",
                },
                "questions": {
                    "type": "object",
                    "minProperties": 1,
                    "description": "map of question id → typed question",
                    "additionalProperties": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string", "enum": ["noul", "choice", "score"]},
                            "instructions": {"type": "string"},
                            "criteria": {
                                "description": "choice: {label: description}; score: [low … high labels]",
                            },
                        },
                        "required": ["type"],
                    },
                },
                "model": {"type": "string", "description": "clef (default) or clef-flash"},
            },
            "required": ["state", "questions"],
            "additionalProperties": False,
        },
    },
    {
        "name": "flow",
        "handler": handlers.flow,
        "risk": "read",
        "description": (
            "List the declarative workflows (flows) the stack can run — small DAGs of "
            "actions (reconcile, keys, backup, eval…) with dependencies and conditions, "
            "e.g. `audit` (full health pass) and `start` (session start). Read-only. Run "
            "one with `conductor flow <name>` or inspect its graph with `--graph`."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "jobs_list",
        "handler": handlers.jobs_list,
        "risk": "read",
        "description": (
            "List every job that executes — the verify gate (event-triggered) plus scheduled "
            "crons / launch agents (clock-triggered) — each with its owner, category, schedule, "
            "last run status (ok/fail/unknown) and age. Optional filters (`owner`, `category`) "
            "and `group_by` (owner/category/kind/host). Read-only; one model shared with the console."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "optional: filter to one owner (project, or 'infra')"},
                "category": {
                    "type": "string",
                    "description": "optional: filter by category (backup/data/notify/monitor/maintenance/verify/app)",
                },
                "group_by": {"type": "string", "description": "optional: group the output (owner/category/kind/host)"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "runs_list",
        "handler": handlers.runs_list,
        "risk": "read",
        "description": (
            "Run history, newest first. Pass `job` (a job id/name, e.g. 'verify-gate') to get "
            "one job's runs, or omit for all. Each run has status, started time, duration and "
            "detail. Read-only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "job": {"type": "string", "description": "optional: job id/name to scope to"},
                "limit": {"type": "integer", "default": 30},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "runs_summary",
        "handler": handlers.runs_summary,
        "risk": "read",
        "description": (
            "Jobs/runs rollup: job count, failing jobs, and run counts by status/trigger. "
            "Read-only — the machine-readable equivalent of the console's Runs KPIs."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "fleet_list",
        "handler": handlers.fleet_list,
        "risk": "read",
        "description": (
            "The run plane — every declared service (the operator tools + project runtimes) "
            "joined with live state. Answers 'what is running locally + on the production host, and is it "
            "alive?'. Joins the fleet manifest + registry.toml service fields against "
            "launchctl / systemd / docker / HTTP probes, and reports drift (down, absent, "
            "unreachable, undeclared). Read-only. Pass offline=true for the declared set with "
            "no probes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "offline": {
                    "type": "boolean",
                    "default": False,
                    "description": "true = declared set only, no live probes (offline/fast)",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "ledger",
        "handler": handlers.ledger,
        "risk": "read",
        "description": (
            "The actuator's action ledger — append-only, hash-chained record of every "
            "applied action: who, what, precondition, effect, verifier result and "
            "rollback. Read-only. Also reports chain integrity (a tampered or truncated "
            "history is detectable). Use to answer 'what did the actuator actually do, "
            "and did it verify?'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 20},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "backup_run",
        "handler": handlers.backup_run,
        "risk": "write",
        "description": (
            "Actuator: run the off-machine restic backup, then prove the snapshot is "
            "fresh. Dry-run by default (apply=false) — runs the precondition and returns "
            "the plan, changing nothing; it refuses if a recent snapshot already exists "
            "unless force=true. apply=true performs the backup (denied until the tool is "
            "allow-listed with `conductor policy --allow-write backup_run`). "
            "Idempotent; recorded in ledger."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "apply": {
                    "type": "boolean",
                    "default": False,
                    "description": "false = dry-run (default); true = run the backup",
                },
                "force": {
                    "type": "boolean",
                    "default": False,
                    "description": "run even if a recent snapshot already exists",
                },
                "timeout": {
                    "type": "integer",
                    "default": 3600,
                    "description": "seconds before the backup run is abandoned",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "projects_register",
        "handler": handlers.projects_register,
        "risk": "write",
        "description": (
            "Actuator: append an existing project directory to registry.toml and "
            "re-render the generated docs. Dry-run by default (apply=false) — checks the "
            "dir is unregistered and returns the plan. apply=true writes the block, "
            "re-renders, and verifies reconcile/render are current; on a failed verify it "
            "restores the prior registry byte-for-byte. Denied until allow-listed with "
            "`conductor policy --allow-write projects_register`. Recorded in "
            "ledger."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "directory name under work/apps or work/research"},
                "title": {"type": "string", "description": "optional human title"},
                "status": {"type": "string", "default": "active"},
                "tier": {"type": "string", "default": "personal", "description": "public | personal | infra"},
                "deploy": {
                    "type": "string",
                    "default": "none",
                    "description": "local | <production-host> | github-pages | none",
                },
                "url": {"type": "string", "description": "optional public URL"},
                "notes": {"type": "string", "description": "optional notes"},
                "apply": {
                    "type": "boolean",
                    "default": False,
                    "description": "false = dry-run (default); true = write the registry",
                },
            },
            "required": ["project"],
            "additionalProperties": False,
        },
    },
    {
        "name": "projects_register_drift",
        "handler": handlers.projects_register_drift,
        "risk": "write",
        "description": (
            "Actuator: register every project directory that exists under work/apps or "
            "work/research but is missing from registry.toml (bulk drift repair). Dry-run "
            "by default — lists the unregistered dirs and returns the plan. apply=true "
            "appends a block per dir, re-renders, and verifies none remain (rollback "
            "restores the prior registry byte-for-byte). Denied until allow-listed with "
            "`conductor policy --allow-write projects_register_drift`. Recorded "
            "in ledger."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "apply": {
                    "type": "boolean",
                    "default": False,
                    "description": "false = dry-run (default); true = register the drift",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "projects_archive",
        "handler": handlers.projects_archive,
        "risk": "write",
        "description": (
            "Actuator: archive a project — flip its status to `archived` in registry.toml "
            "and move its directory to work/archive/. Dry-run by default; apply=true writes "
            "and moves, then verifies the status, the move, and that the docs re-render "
            "(rollback restores the registry and moves the directory back). Denied until "
            "allow-listed with `conductor policy --allow-write projects_archive`. "
            "Recorded in ledger."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "registered project name to archive"},
                "apply": {
                    "type": "boolean",
                    "default": False,
                    "description": "false = dry-run (default); true = archive (flip status + move dir)",
                },
            },
            "required": ["project"],
            "additionalProperties": False,
        },
    },
    {
        "name": "docs_render",
        "handler": handlers.docs_render,
        "risk": "write",
        "description": (
            "Actuator: regenerate the workspace registry docs (registry/REGISTRY.md + "
            "README.md) when stale. Dry-run by default; apply=true re-renders and verifies "
            "the docs are current (rollback restores the prior files). Denied until "
            "allow-listed with `conductor policy --allow-write docs_render`. "
            "Recorded in ledger."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "apply": {
                    "type": "boolean",
                    "default": False,
                    "description": "false = dry-run (default); true = re-render the docs",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "keys_retire",
        "handler": handlers.keys_retire,
        "risk": "write",
        "description": (
            "Actuator: retire location-less keys whose notes mark them revoked/orphaned. "
            "Dry-run by default; apply=true rewrites the keys registry and verifies no dead "
            "keys remain (rollback restores the prior registry byte-for-byte). Denied until "
            "allow-listed with `conductor policy --allow-write keys_retire`. "
            "Recorded in ledger."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "apply": {
                    "type": "boolean",
                    "default": False,
                    "description": "false = dry-run (default); true = retire the dead keys",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "docker_prune",
        "handler": handlers.docker_prune,
        "risk": "write",
        "description": (
            "Actuator: prune dangling Docker images + build cache. Dry-run by default; "
            "apply=true runs both prunes and verifies they succeeded (irreversible — no "
            "rollback). Denied until allow-listed with "
            "`conductor policy --allow-write docker_prune`. Recorded in "
            "ledger."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "apply": {
                    "type": "boolean",
                    "default": False,
                    "description": "false = dry-run (default); true = run the prunes",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "worktree_add",
        "handler": handlers.worktree_add,
        "risk": "write",
        "description": (
            "Actuator: create an isolated git worktree + branch (`<agent>/<task>`) for a "
            "parallel agent task, next to the project's main checkout. This is the one-shot "
            "form of the work/notes/PARALLEL-AGENTS.md recipe — isolation becomes one call. "
            "Dry-run by default (apply=false) — checks the project is a git repo, the path is "
            "free and the branch is new, and returns the plan. apply=true creates it (denied "
            "until allow-listed with `conductor policy --allow-write worktree_add`). "
            "Reversible (rollback removes the tree + branch); recorded in ledger."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "project name (dir under work/apps or work/research)"},
                "task": {"type": "string", "description": "short task slug (e.g. print-pdf), no slashes"},
                "agent": {"type": "string", "default": "agent", "description": "agent prefix for the branch"},
                "apply": {
                    "type": "boolean",
                    "default": False,
                    "description": "false = dry-run (default); true = create the worktree",
                },
            },
            "required": ["project", "task"],
            "additionalProperties": False,
        },
    },
    {
        "name": "worktree_remove",
        "handler": handlers.worktree_remove,
        "risk": "write",
        "description": (
            "Actuator: remove a task's worktree (the branch survives — re-add to restore the "
            "files). Pass force=true to remove a dirty tree. Dry-run by default; apply=true "
            "removes it (denied until allow-listed with "
            "`conductor policy --allow-write worktree_remove`). Irreversible for "
            "untracked files; recorded in ledger."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "project name"},
                "task": {"type": "string", "description": "the task slug used at add time"},
                "force": {
                    "type": "boolean",
                    "default": False,
                    "description": "remove even if the worktree has uncommitted changes",
                },
                "apply": {
                    "type": "boolean",
                    "default": False,
                    "description": "false = dry-run (default); true = remove the worktree",
                },
            },
            "required": ["project", "task"],
            "additionalProperties": False,
        },
    },
    {
        "name": "crp_file",
        "handler": handlers.crp_file,
        "risk": "write",
        "description": (
            "File a Consultation Request Pack — a durable question for the human, for when "
            "you hit ambiguity you must not resolve alone. Use this instead of guessing. "
            "The CRP outlives your session, appears at the top of `conductor brief`, and "
            "states `default_action`: what you will do if it is never answered. Answering "
            "is human-only — you cannot answer your own CRP. Dry-run by default; apply=true "
            "files it (denied until `conductor policy --allow-write crp_file`)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "the bounded question — one decision, answerable",
                },
                "options": {
                    "type": "array",
                    "description": "the choices, each with its tradeoff",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string"},
                            "tradeoff": {"type": "string"},
                        },
                        "required": ["label"],
                        "additionalProperties": False,
                    },
                },
                "context": {
                    "type": "string",
                    "description": "what you already know: files, state, what you tried",
                },
                "blast_radius": {
                    "type": "string",
                    "description": "reversible? who else is affected?",
                },
                "default_action": {
                    "type": "string",
                    "description": "what you will do if no answer arrives — state it, don't guess silently",
                },
                "default_after": {
                    "type": "string",
                    "description": "deadline for the default: YYYY-MM-DD or 'YYYY-MM-DD HH:MM'",
                },
                "project": {"type": "string", "description": "optional: the project this concerns"},
                "apply": {
                    "type": "boolean",
                    "default": False,
                    "description": "false = dry-run (default); true = file the CRP",
                },
            },
            "required": ["question"],
            "additionalProperties": False,
        },
    },
]


def by_name(name: str) -> dict | None:
    """The one tool lookup — schema validation, policy and dispatch all resolve through it.

    Lives next to the data so the transport, the permission layer and the gate share a
    single resolver instead of each scanning the registry their own way.
    """
    return next((t for t in TOOLS if t["name"] == name), None)


def wire_tools() -> list[dict]:
    """The serializable projection of the registry — everything except the handler.

    `handler` is a callable and never goes on the wire; `tools/list` must return plain
    JSON. Keeping the projection here (next to the data it projects) means the registry
    stays one structure while the wire view stays pure data — and `conductor.gate`
    asserts the result actually serializes, so a future non-serializable key fails the
    gate instead of the client.
    """
    return [{k: v for k, v in tool.items() if k != "handler"} for tool in TOOLS]
