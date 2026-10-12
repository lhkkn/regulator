"""Backup freshness — is the off-machine restic snapshot recent? Read-only.

The mac's irreplaceable data is backed up daily to Backblaze B2 via restic
(`ops/backup/backup.sh`). This module reports the age of the latest snapshot so `brief`
can flag a backup that has silently stopped running — the failure mode that turns a bad
day into a loss. A backup you don't monitor isn't a backup.

The **server** half (production host → object storage) is checked the same
way: `server_sqlite()` reports the age of the newest DB snapshot over one SSH round-trip.

Credentials come from the macOS Keychain (`conductor.keychain`), repo details from
`conductor.config`. Defensive: any failure degrades to ok=False with a short reason, never
raises. Memoized.

    python -m conductor backup          # standalone status
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from datetime import UTC, datetime

from conductor import config, hostenv, keychain, proc

# launchd runs with a minimal PATH that excludes Homebrew, so PATH alone misses restic.
_RESTIC_FALLBACKS = ("/opt/homebrew/bin/restic", "/usr/local/bin/restic")
_KC_SERVICE = "restic-mac-backup"
_MEMO: tuple[float, dict] | None = None
_SERVER_MEMO: tuple[float, dict] | None = None


def _restic_bin() -> str | None:
    return proc.find("restic", *_RESTIC_FALLBACKS)


def _drill_path():
    """Path to the restore-drill record (env-redirectable via config)."""
    return config.state_file("restore-drill.json")


def latest(fresh: bool = False) -> dict:
    """Age of the most recent restic snapshot. Never raises. Memoized `_TTL` seconds.

    Record: {ok, time, age_hours, error}. ok=True only when a snapshot was read and
    its timestamp parsed.
    """
    global _MEMO
    now = time.time()
    if not fresh and _MEMO is not None and now - _MEMO[0] < config.BACKUP_MEMO_SECONDS:
        return _MEMO[1]

    rec: dict = {"ok": False, "time": None, "age_hours": None, "error": None}
    restic = _restic_bin()
    repo = config.backup_repo()
    if not repo:
        rec["error"] = "backup not configured (set backup_repo in config.toml)"
    elif not restic:
        rec["error"] = "restic not installed"
    else:
        key_id = keychain.get(_KC_SERVICE, "b2-key-id")
        key = keychain.get(_KC_SERVICE, "b2-key")
        pw = keychain.get(_KC_SERVICE, "repo-password")
        if not (key_id and key and pw):
            rec["error"] = "keychain credentials missing"
        else:
            # restic reaches B2 via the S3-compatible endpoint (AWS-style creds from
            # the Keychain), so there is no rclone.conf and no rclone child to leak.
            env = dict(
                os.environ,
                RESTIC_PASSWORD=pw,
                RESTIC_REPOSITORY=repo,
                AWS_ACCESS_KEY_ID=key_id,
                AWS_SECRET_ACCESS_KEY=key,
                AWS_DEFAULT_REGION=config.setting("backup_region", "us-east-005"),
                PATH="/opt/homebrew/bin:/usr/local/bin:" + os.environ.get("PATH", "/usr/bin:/bin"),
            )
            # start_new_session: on timeout kill the whole group, or restic's child
            # process is orphaned and lingers (a storm when B2 is slow — each probe
            # leaks one). The bounded timeout keeps brief fast.
            proc_run: subprocess.CompletedProcess | None = None
            try:
                child = subprocess.Popen(
                    [restic, "snapshots", "--json", "--latest", "1"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                    start_new_session=True,
                )
                try:
                    out, err = child.communicate(timeout=config.BACKUP_PROBE_TIMEOUT)
                    proc_run = subprocess.CompletedProcess(child.args, child.returncode, out, err)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
                    child.communicate()
                    rec["error"] = "restic timed out"
            except OSError as exc:
                rec["error"] = f"restic failed to start: {exc}"
            if proc_run is not None:
                if proc_run.returncode != 0:
                    err = (proc_run.stderr.strip().splitlines() or ["restic failed"])[-1]
                    rec["error"] = err[:100]
                else:
                    try:
                        snaps = json.loads(proc_run.stdout or "[]")
                        # `--latest 1` returns the newest snapshot *per host*; two Macs
                        # share this repo, so take the max — snaps[0] can be the older host.
                        when = max((s.get("time") for s in snaps if s.get("time")), default=None)
                        if when:
                            dt = datetime.fromisoformat(when.replace("Z", "+00:00"))
                            age = (datetime.now(UTC) - dt).total_seconds() / 3600
                            rec.update(ok=True, time=when, age_hours=round(age, 1))
                        else:
                            rec["error"] = "no snapshots"
                    except (ValueError, KeyError, IndexError, TypeError) as exc:
                        rec["error"] = f"parse: {exc}"

    _MEMO = (now, rec)
    return rec


def drill() -> dict:
    """Age of the last verified restore drill (`backup.sh --drill`). Never raises.

    Record: {ok, time, age_days, error}. ok=False with error 'no drill recorded'
    when none has ever run.
    """
    try:
        with open(_drill_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        when = data.get("restored_at")
        dt = datetime.fromisoformat(when.replace("Z", "+00:00"))
        age = (datetime.now(UTC) - dt).total_seconds() / 86400
        return {"ok": bool(data.get("verified")), "time": when, "age_days": round(age, 1), "error": None}
    except FileNotFoundError:
        return {"ok": False, "time": None, "age_days": None, "error": "no drill recorded"}
    except (OSError, ValueError, AttributeError, TypeError) as exc:
        return {"ok": False, "time": None, "age_days": None, "error": str(exc)[:80]}


def server_sqlite(host: str | None = None, fresh: bool = False) -> dict:
    """Age of the newest server DB snapshot in object storage.

    One SSH round-trip to the prod host listing the configured `server_backup_remote`.
    Never raises. Record: {ok, time, age_hours, count, error, unprobed}. `unprobed`
    means the host was unreachable — reported as neutral (not a failure) so a laptop
    off-network doesn't alarm, while a reachable-but-empty/old bucket still flags.
    Memoized `config.BACKUP_MEMO_SECONDS`.
    """
    global _SERVER_MEMO
    now = time.time()
    if not fresh and _SERVER_MEMO is not None and now - _SERVER_MEMO[0] < config.BACKUP_MEMO_SECONDS:
        return _SERVER_MEMO[1]

    rec: dict = {
        "ok": False,
        "time": None,
        "age_hours": None,
        "count": 0,
        "dbs": 0,
        "error": None,
        "unprobed": False,
    }
    host = host or config.production_host()
    remote_path = config.setting("server_backup_remote")
    if not host or not remote_path:
        rec.update(error="server backup not configured (production_host, server_backup_remote)", unprobed=True)
        _SERVER_MEMO = (now, rec)
        return rec
    # Parse the UTC stamp embedded in each filename (`<label>-<YYYYmmddTHHMMSSZ>.db`)
    # rather than rclone's modtime — the server's rclone (v1.50) has no `--utc`, and
    # the filename stamp is canonical anyway.
    remote = f"rclone lsf {remote_path} --max-depth 1"
    try:
        r = hostenv.ssh(host, remote, timeout=30)
    except subprocess.TimeoutExpired:
        rec.update(error="ssh timed out", unprobed=True)
    else:
        if r.returncode != 0:
            err = (r.stderr.strip().splitlines() or ["ssh failed"])[-1]
            rec.update(error=err[:100], unprobed=True)
        else:
            newest = None
            labels: set[str] = set()
            for name in r.stdout.splitlines():
                # `<label>-<stamp>.db`, and labels themselves can contain hyphens —
                # so the stamp is anchored at the end, not split on
                # the first dash. `count` is therefore *snapshots retained*, not
                # databases; reporting one as the other overstated the estate by ~11x.
                m = re.search(r"^(.*)-(\d{8}T\d{6}Z)\.db$", name.strip())
                if not m:
                    continue
                rec["count"] += 1
                labels.add(m.group(1))
                try:
                    dt = datetime.strptime(m.group(2), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
                except ValueError:
                    continue
                if newest is None or dt > newest:
                    newest = dt
            rec["dbs"] = len(labels)
            if newest is None:
                rec["error"] = "no snapshots"
            else:
                age = (datetime.now(UTC) - newest).total_seconds() / 3600
                rec.update(ok=True, time=newest.isoformat(), age_hours=round(age, 1))

    _SERVER_MEMO = (now, rec)
    return rec


def main() -> int:
    rec = latest(fresh=True)
    if rec["ok"]:
        print(f"backup OK — latest snapshot {rec['time']} ({rec['age_hours']}h ago)")
    else:
        print(f"backup PROBLEM — {rec['error']}")
    d = drill()
    if d["ok"]:
        print(f"restore drill OK — last verified {d['age_days']} days ago")
    else:
        print(f"restore drill: {d['error']}")
    s = server_sqlite(fresh=True)
    if s["ok"]:
        print(f"server DBs OK — {s['count']} in B2, newest {s['age_hours']}h ago")
    elif s["unprobed"]:
        print(f"server DBs unprobed — {s['error']}")
    else:
        print(f"server DBs PROBLEM — {s['error']}")
    return 0 if (rec["ok"] and (s["ok"] or s["unprobed"])) else 1


if __name__ == "__main__":
    raise SystemExit(main())
