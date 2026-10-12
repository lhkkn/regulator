"""Golden-set eval harness for the Conductor MCP surface.

Two tiers, one runner:

* **Tier-1 (deterministic).** Each case is a JSON-RPC request fed to `server.handle()`
  plus deterministic expectations (substring / exact / error-code / is-error). No model.
  This is the *contract* gate — the model may change, the interface agents depend on
  must not regress. Same discipline as `make check`, generalized to the agent surface.

* **Tier-2 (judged).** A case may add `"rubric": "<prompt>"`; the tool output is judged
  YES/NO by `conductor.judge` — Clef (Cloudflare Workers AI) first, then a local ollama
  model. Needs a judge; when none is reachable the case is **skipped** (reported, not
  failed) — the harness stays useful offline. Gate = no *executed* case fails.

Golden sets: <project>/eval/golden.jsonl (tier-1) and eval/tier2.jsonl (tier-2);
one JSON object per line, `#` comments allowed.

    python3 -m conductor eval                 # tier-1 contract gate
    python3 -m conductor eval --tier 2        # judged task-level gate (skips if no judge)
    python3 -m conductor eval --json          # machine-readable summary
    python3 -m conductor eval --golden <path> # an arbitrary golden file
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from conductor import judge
from conductor.mcp import server

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "eval" / "golden.jsonl"
GOLDEN_T2 = ROOT / "eval" / "tier2.jsonl"
GATE = 1.0  # executed deterministic/rubric assertions must all pass


class Case:
    __slots__ = ("name", "request", "expect")

    def __init__(self, name: str, request: Any, expect: dict) -> None:
        self.name = name
        self.request = request
        self.expect = expect


def load_golden(path: Path) -> list[Case]:
    cases: list[Case] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            obj = json.loads(line)
            cases.append(Case(obj["name"], obj["request"], obj.get("expect", {})))
    return cases


def _response_text(resp: dict | None) -> tuple[str | None, bool, int | None]:
    """Extract (text, is_error, error_code) from a `server.handle()` response.

    `text` is None for an RPC error. Tool results with a `content` block yield the
    joined text; structured results (e.g. `tools/list`) are JSON-serialized so they
    can be searched with substring assertions too.
    """
    if resp is None:
        return None, False, None
    if "error" in resp:
        return None, True, resp["error"].get("code")
    result = resp.get("result", {})
    is_err = bool(result.get("isError"))
    content = result.get("content")
    if isinstance(content, list):
        text = "".join(c.get("text", "") for c in content if isinstance(c, dict))
    else:
        text = json.dumps(result, ensure_ascii=False) if isinstance(result, dict) else str(result)
    return text, is_err, None


# --- judge ------------------------------------------------------------------
#
# The backends live in `conductor/judge.py` (Clef, then ollama).


def _parse_yes_no(resp: str) -> bool:
    """Delegate to `conductor.judge.parse_yes_no` (kept here for callers/tests)."""
    return judge.parse_yes_no(resp)


# --- judging ----------------------------------------------------------------


def _judge(case: Case, resp: dict | None) -> tuple[str, str]:
    """Return (status, detail) with status in {"pass","fail","skip"}."""
    text, is_err, code = _response_text(resp)
    exp = case.expect

    # deterministic assertions (tier-1, always available)
    if "is_error" in exp and bool(is_err) != bool(exp["is_error"]):
        return "fail", f"expected is_error={exp['is_error']}, got is_error={is_err}"
    if "error_code" in exp:
        if code != exp["error_code"]:
            return "fail", f"expected error_code={exp['error_code']}, got {code}"
        if code is not None:
            return "pass", ""
    if "expect_response" in exp and not exp["expect_response"]:
        return ("pass", "") if text is None else ("fail", "expected no response, got one")
    if text is None:
        return "fail", f"expected a response with content, got none (is_error={is_err})"
    for sub in exp.get("contains", []):
        if sub not in text:
            return "fail", f"missing expected substring {sub!r}"
    for sub in exp.get("not_contains", []):
        if sub in text:
            return "fail", f"unexpected substring {sub!r} present"
    if "exact" in exp and text != exp["exact"]:
        return "fail", f"exact mismatch (got {text[:120]!r})"

    # tier-2: judged rubric (Clef, then ollama — see conductor/judge.py)
    if "rubric" in exp:
        if not judge.available():
            return "skip", "no judge reachable (set CONDUCTOR_CLEF_TOKEN / link the Cloudflare key, or start ollama)"
        try:
            ok = judge.verdict(exp["rubric"], text)
        except judge.NoJudge as exc:
            return "skip", f"judge unavailable: {exc}"
        except Exception as exc:  # noqa: BLE001 — judge failure is a skip, not a verdict
            return "skip", f"judge error: {type(exc).__name__}: {exc}"
        return ("pass", "") if ok else ("fail", f"rubric not satisfied: {exp['rubric'][:90]}")
    return "pass", ""


def run_cases(cases: list[Case]) -> list[dict]:
    results: list[dict] = []
    for c in cases:
        try:
            resp = server.handle(c.request)
        except Exception as exc:  # the SUT must never raise out of the runner
            results.append(
                {"name": c.name, "status": "fail", "detail": f"raised {type(exc).__name__}: {exc}", "out": ""}
            )
            continue
        status, detail = _judge(c, resp)
        text, is_err, code = _response_text(resp)
        results.append(
            {
                "name": c.name,
                "status": status,
                "pass": status == "pass",
                "detail": detail,
                "out": (text or f"[no content, is_error={is_err}, code={code}]")[:160],
            }
        )
    return results


def summarize(results: list[dict]) -> dict:
    total = len(results)
    passed = sum(1 for r in results if r["status"] == "pass")
    failed = sum(1 for r in results if r["status"] == "fail")
    skipped = sum(1 for r in results if r["status"] == "skip")
    executed = passed + failed
    rate = round(passed / executed, 4) if executed else 0.0
    return {
        "cases": total,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "executed": executed,
        "pass_rate": rate,
        "gate": GATE,
        "ok": failed == 0 and total > 0,
    }


def run(path: Path = GOLDEN) -> tuple[bool, str]:
    """Run a golden set and return (gate_ok, human-readable report)."""
    cases = load_golden(path)
    results = run_cases(cases)
    summary = summarize(results)
    lines = [
        f"eval: {summary['passed']}/{summary['executed']} executed passed "
        f"({summary['skipped']} skipped, rate {summary['pass_rate']:.0%}, "
        f"gate {summary['gate']:.0%})"
    ]
    for r in results:
        if r["status"] == "fail":
            lines.append(f"  FAIL {r['name']}: {r['detail']}")
    return summary["ok"], "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    path = GOLDEN
    if "--tier" in argv:
        path = GOLDEN_T2 if argv[argv.index("--tier") + 1] == "2" else GOLDEN
    if "--golden" in argv:
        path = Path(argv[argv.index("--golden") + 1])
    cases = load_golden(path)
    results = run_cases(cases)
    summary = summarize(results)

    if "--json" in argv:
        print(json.dumps({"summary": summary, "cases": results}, ensure_ascii=False, indent=2))
        return 0 if summary["ok"] else 1

    for r in results:
        mark = {"pass": "ok  ", "fail": "FAIL", "skip": "skip"}[r["status"]]
        print(f"  [{mark}] {r['name']}")
        if r["status"] != "pass" and r["detail"]:
            print(f"          {r['detail']}")
    print(
        f"\neval: {summary['passed']}/{summary['executed']} executed passed "
        f"({summary['skipped']} skipped, rate {summary['pass_rate']:.0%}, "
        f"gate {summary['gate']:.0%})"
    )
    if not summary["ok"]:
        print("eval: gate NOT met — the contract is not safe to ship")
        return 1
    print("eval: gate met")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
