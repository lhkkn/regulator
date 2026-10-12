"""Conductor tokens module — AI spend across local agent CLI logs (v1 scope: `tokens/`).

Read-only parsers over the logs the agents already write; nothing is instrumented or
mutated ("discover, don't hand-maintain"):

- kimi-code  ~/.kimi-code/sessions/**/wire.jsonl   — "usage.record" lines, turn scope
- claude     ~/.claude/projects/*/*.jsonl          — message.usage (dedupe by message id)
- opencode   ~/.local/share/opencode/opencode.db   — message.data JSON (carries real cost)

All costs are ESTIMATES unless a source records the real figure (opencode does).
Managed/subscription plans (kimi-code) are $0-marginal and marked "plan"; local models
(ollama, lmstudio) are "free". The pricing table is hand-maintained and goes stale —
treat dollar figures as indicative, never billing-grade.
"""

from conductor.tokens import balances  # noqa: F401
from conductor.tokens.spend import daily, daily_by_model, sources, summary  # noqa: F401
