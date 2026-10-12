"""Local models — the privacy/offline lane.

The house rule is local-first: personal data never leaves the machine. Cloud models are
fine for generic work, but some jobs (private notes, keys-adjacent reasoning, the eval
judge) should run here. This wires a local runtime (ollama) as a first-class part of the
stack: detect it, pull a model, keep the server running, and offer one-shot inference.

The same runtime doubles as the tier-2 eval judge (`conductor eval --tier 2`), so the
quality gate works with no network and no cloud spend.

    python -m conductor local                 # runtime + models + judge readiness
    python -m conductor local --pull          # pull the default judge model
    python -m conductor local --install       # keep the server running (LaunchAgent)
    python -m conductor local --ask "prompt"  # one-shot local completion
    python -m conductor local --model <name>  # override the model
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.request

from conductor import config, launchagent, proc

_URL = config.OLLAMA_URL
DEFAULT_MODEL = config.JUDGE_MODEL
_LABEL = "com.conductor.ollama"
_OP_FALLBACKS = ("/opt/homebrew/bin/ollama", "/usr/local/bin/ollama", "/opt/homebrew/opt/ollama/bin/ollama")


def _ollama() -> str | None:
    return proc.find("ollama", *_OP_FALLBACKS)


def _get(path: str, timeout: int = 2):
    with urllib.request.urlopen(f"{_URL}{path}", timeout=timeout) as r:
        return json.loads(r.read())


def server_up() -> bool:
    try:
        _get("/api/version")
        return True
    except Exception:  # noqa: BLE001
        return False


def models() -> list[str]:
    try:
        return [m.get("name", "") for m in _get("/api/tags").get("models", [])]
    except Exception:  # noqa: BLE001
        return []


def generate(prompt: str, model: str = DEFAULT_MODEL, timeout: int = 180) -> str:
    """One-shot local completion. Raises on failure."""
    body = json.dumps({"model": model, "prompt": prompt, "stream": False, "options": {"temperature": 0}}).encode()
    req = urllib.request.Request(f"{_URL}/api/generate", data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read()).get("response", "").strip()


def status() -> str:
    bin_ = _ollama()
    lines = ["local models (privacy/offline lane)"]
    lines.append(f"  runtime:  {'ollama ' + _ver() if bin_ else 'not installed'}")
    lines.append(f"  server:   {'up at ' + _URL if server_up() else 'down'}")
    ms = models()
    lines.append(f"  models:   {', '.join(ms) if ms else '— none pulled'}")
    judge_ready = bool(bin_) and server_up() and any(DEFAULT_MODEL.split(":")[0] in m for m in ms)
    lines.append(f"  judge:    {DEFAULT_MODEL} — {'ready' if judge_ready else 'not ready'}")
    lines.append("  (the judge powers `conductor eval --tier 2`; pulls are local-first)")
    return "\n".join(lines)


def _ver() -> str:
    ollama = _ollama()
    if not ollama:
        return ""
    try:
        return (
            subprocess.run([ollama, "--version"], capture_output=True, text=True, timeout=10)
            .stdout.strip()
            .replace("ollama version is ", "v")
        )
    except Exception:  # noqa: BLE001
        return ""


def pull(model: str = DEFAULT_MODEL) -> int:
    ollama = _ollama()
    if not ollama:
        print("local: ollama not installed (`brew install ollama`)")
        return 1
    if not server_up():
        print("local: server not running — start with `brew services start ollama` or `regulator local --install`")
        return 1
    print(f"local: pulling {model} …")
    return subprocess.run([ollama, "pull", model]).returncode


def _plist_text() -> str:
    program = [_ollama() or "/opt/homebrew/opt/ollama/bin/ollama", "serve"]
    return launchagent.render(
        _LABEL, program, run_at_load=True, keep_alive=True, log=str(config.logs_dir() / "ollama.log")
    )


def install() -> str:
    if not _ollama():
        return "ollama not installed"
    launchagent.install(_LABEL, _plist_text(), log=str(config.logs_dir() / "ollama.log"))
    return f"installed {_LABEL} (ollama serve, kept alive)"


def uninstall() -> str:
    launchagent.uninstall(_LABEL)
    return f"removed {_LABEL}"


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    model = argv[argv.index("--model") + 1] if "--model" in argv else DEFAULT_MODEL

    if "--install" in argv:
        print(install())
        return 0
    if "--uninstall" in argv:
        print(uninstall())
        return 0
    if "--pull" in argv:
        return pull(model)
    if "--ask" in argv:
        prompt = argv[argv.index("--ask") + 1]
        if not server_up():
            print("local: server not running")
            return 1
        try:
            print(generate(prompt, model))
        except Exception as exc:  # noqa: BLE001
            print(f"local: generate failed: {exc}")
            return 1
        return 0

    print(status())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
