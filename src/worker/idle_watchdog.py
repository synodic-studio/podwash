"""Mac-side idle watchdog — first line of defense for a stuck worker.

The crash wrapper (`src.worker.wrapper`) only fires when the worker
process is *crashing*. A worker that is alive-but-idle (hung HTTP call,
network partition, deadlocked Whisper, mis-claimed token) never trips
it — we'd silently stop pulling work while launchd happily reports
"running."

This module is the layer above that: a separate launchd job runs us on
an interval, we ask the Vultr API how the queue looks, and if there's
backlog with no recent claim we hand it to `python -m src.heal` exactly
like the crash wrapper does. Telegram only fires on heal escalation.

Stdlib-only on purpose — runs under /usr/bin/python3 so a broken
project venv can't keep the watchdog itself from booting (the whole
point is to detect and recover from a broken venv).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Heal CLI exit codes — kept literal so this file doesn't have to import
# the project (the same trick wrapper.py uses).
HEAL_EXIT_SUCCESS = 0
HEAL_EXIT_ESCALATE = 2
HEAL_EXIT_TIMEOUT = 3
HEAL_EXIT_UNAVAILABLE = 4

# Trip threshold — match server-side watchdog so the two layers agree.
DEFAULT_NO_CLAIM_MINUTES = 30
# Don't fire heal more than once per cooldown window — the heal CLI
# itself has a daily cap, but each run costs Anthropic credits and
# spawns a Claude session, so we add a per-process throttle too.
DEFAULT_COOLDOWN_SECONDS = 1800
# A trip has to survive two consecutive ticks before we heal. The worker
# idles 120s between polls and ticks are 300s apart, so a single tick can
# land in the gap between one episode completing and the next being
# claimed — a healthy state that looks identical to a stall. Window is
# wide enough that one skipped tick doesn't reset the streak.
DEFAULT_CONFIRM_WINDOW_SECONDS = 1200


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _resolve_token() -> str:
    """Worker token from env, falling back to `pass show podwash-worker-token`."""
    if env := os.environ.get("WORKER_TOKEN"):
        return env.strip()
    try:
        r = subprocess.run(
            ["pass", "show", "podwash-worker-token"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return ""


def _fetch_health(server_url: str, token: str, timeout: float = 15.0) -> dict:
    """Hit /api/jobs/queue/health and return the snapshot dict."""
    url = server_url.rstrip("/") + "/api/jobs/queue/health"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _silent_minutes(last_claim_at: str | None, now: datetime) -> float:
    """How long since any worker last claimed. inf if never.

    Normalizes both sides to naive UTC before subtracting so a mix of
    aware/naive timestamps (sqlite may have stored either) doesn't
    raise ``TypeError`` and crash the tick.
    """
    if not last_claim_at:
        return float("inf")
    try:
        last = datetime.fromisoformat(last_claim_at)
    except (ValueError, TypeError):
        return float("inf")
    try:
        if last.tzinfo is not None:
            last = last.astimezone(timezone.utc).replace(tzinfo=None)
        ref = now
        if ref.tzinfo is not None:
            ref = ref.astimezone(timezone.utc).replace(tzinfo=None)
        return (ref - last).total_seconds() / 60
    except (TypeError, ValueError):
        return float("inf")


def evaluate(
    snapshot: dict,
    *,
    no_claim_minutes: int,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """Return (should_heal, reason). Pure function — easy to test."""
    now = now or datetime.now()
    pending = int(snapshot.get("pending_count") or 0)
    if pending == 0:
        return False, "queue empty — nothing to do"
    silent = _silent_minutes(snapshot.get("last_claim_at"), now)
    if silent < no_claim_minutes:
        return False, (
            f"queue has {pending} pending but last claim was "
            f"{silent:.0f} min ago (under {no_claim_minutes} min threshold)"
        )
    silent_str = (
        "never seen a claim" if silent == float("inf") else f"{silent:.0f} min"
    )
    return True, (
        f"{pending} episode(s) pending and worker has been silent for "
        f"{silent_str} (>= {no_claim_minutes} min threshold)"
    )


def _within_cooldown(stamp_file: Path, cooldown_seconds: int) -> bool:
    """True if we already fired heal less than cooldown_seconds ago."""
    if not stamp_file.exists():
        return False
    try:
        last = float(stamp_file.read_text().strip())
    except (OSError, ValueError):
        return False
    return (time.time() - last) < cooldown_seconds


def _stamp_now(stamp_file: Path) -> None:
    stamp_file.parent.mkdir(parents=True, exist_ok=True)
    stamp_file.write_text(f"{time.time():.0f}\n")


def _confirm_repeat_trip(
    trip_file: Path,
    snapshot: dict,
    *,
    window_seconds: int,
    now: float | None = None,
) -> bool:
    """True when the previous tick tripped on the same backlog.

    Records this tick's trip either way, so the streak builds up across
    runs of this one-shot process. A different `oldest_pending_id` means
    the queue moved on, which is evidence the worker is alive, so the
    streak restarts rather than carrying over.
    """
    now = time.time() if now is None else now
    current = snapshot.get("oldest_pending_id")
    previous = None
    if trip_file.exists():
        try:
            prior = json.loads(trip_file.read_text())
            if now - float(prior["at"]) <= window_seconds:
                previous = prior.get("oldest_pending_id")
        except (OSError, ValueError, KeyError, TypeError):
            previous = None

    trip_file.parent.mkdir(parents=True, exist_ok=True)
    trip_file.write_text(json.dumps({"oldest_pending_id": current, "at": now}))
    return previous is not None and previous == current


def _write_incident(state_dir: Path, snapshot: dict, reason: str) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    now = time.time()
    path = state_dir / f"idle-incident-{int(now)}.txt"
    body = (
        "podwash-worker idle watchdog tripped\n"
        f"timestamp: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now))}\n"
        f"reason: {reason}\n"
        "\n"
        "Queue snapshot from /api/jobs/queue/health:\n"
        f"  pending_count:     {snapshot.get('pending_count')}\n"
        f"  in_flight_count:   {snapshot.get('in_flight_count')}\n"
        f"  last_claim_at:     {snapshot.get('last_claim_at')}\n"
        f"  last_claim_by:     {snapshot.get('last_claim_by')}\n"
        f"  oldest_pending_id: {snapshot.get('oldest_pending_id')}\n"
        "\n"
        "The crash wrapper hasn't fired (worker isn't crash-looping), so the\n"
        "process is likely alive but stuck. Look at:\n"
        "  - the worker launchd job (running?)\n"
        "  - tail of the worker log under ~/Library/Logs/podwash-worker/\n"
        "  - network reachability to the server (e.g. tailscale status)\n"
        "  - is the worker process hung mid-Whisper / mid-HTTP?\n"
        "Possible fixes: kickstart the worker launchd job,\n"
        "uv sync --extra worker, restore network, fix config.yml.\n"
        "\n"
        "Confirm the worker is actually idle before restarting anything.\n"
        "A busy worker is mid-Whisper, not hung: it burns whole cores and\n"
        "holds a $TMPDIR/podwash-<episode_id>-* directory. Sample its CPU\n"
        "over ~20s and list that directory before you kickstart — a restart\n"
        "throws away an in-flight episode. Worker stdout is block-buffered,\n"
        "so a quiet log is not evidence of a stalled worker.\n"
    )
    path.write_text(body)
    return path


def _run_heal(
    *,
    incident_path: Path,
    repo: Path,
    log_dir: Path,
    timeout_seconds: int,
) -> int:
    """Invoke the heal CLI under THIS interpreter (system Python is fine —
    src.heal is stdlib + uses subprocess to spawn `claude`)."""
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "src.heal",
            "--incident-file",
            str(incident_path),
            "--repo",
            str(repo),
            "--log-dir",
            str(log_dir),
            "--timeout",
            str(timeout_seconds),
            "--telegram-on-escalate",
        ],
        cwd=str(repo),
    )
    return proc.returncode


def _direct_telegram_fallback(*, repo: Path, reason: str) -> None:
    """Last resort if the heal CLI itself returned an unknown rc."""
    sys.path.insert(0, str(repo))
    try:
        from src.alerting import send_alert
    except Exception as exc:
        print(
            f"[idle-watchdog] could not import alerting for fallback: {exc}",
            file=sys.stderr,
        )
        return
    try:
        send_alert(
            subsystem="worker-idle-watchdog",
            kind="self-heal infrastructure failure",
            problem=(
                f"Idle watchdog tripped ({reason}) but the heal CLI "
                "returned an unexpected exit code."
            ),
            fix=(
                "The watchdog itself or src.heal is broken — read "
                "~/Library/Logs/podwash-worker/idle-watchdog.err and "
                "~/Library/Logs/podwash-worker/heal/."
            ),
        )
    except Exception as exc:
        print(f"[idle-watchdog] alerting fallback raised: {exc}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="podwash-idle-watchdog")
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path(os.environ.get("REPO_DIR", Path(__file__).resolve().parents[2])),
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=Path(
            os.environ.get(
                "LOG_DIR", str(Path.home() / "Library" / "Logs" / "podwash-worker")
            )
        ),
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(os.environ.get("STATE_DIR", "/tmp/podwash-idle-watchdog")),
    )
    parser.add_argument(
        "--server-url",
        default=os.environ.get("WORKER_SERVER_URL", "http://localhost:8080"),
    )
    parser.add_argument(
        "--no-claim-minutes",
        type=int,
        default=_env_int("IDLE_NO_CLAIM_MINUTES", DEFAULT_NO_CLAIM_MINUTES),
    )
    parser.add_argument(
        "--cooldown-seconds",
        type=int,
        default=_env_int("IDLE_COOLDOWN_SECONDS", DEFAULT_COOLDOWN_SECONDS),
    )
    parser.add_argument(
        "--confirm-window-seconds",
        type=int,
        default=_env_int(
            "IDLE_CONFIRM_WINDOW_SECONDS", DEFAULT_CONFIRM_WINDOW_SECONDS
        ),
    )
    parser.add_argument(
        "--heal-timeout",
        type=int,
        default=_env_int("HEAL_TIMEOUT_SECONDS", 600),
    )
    args = parser.parse_args(argv)

    token = _resolve_token()
    if not token:
        print(
            "[idle-watchdog] no WORKER_TOKEN — cannot reach server, exiting 0",
            file=sys.stderr,
        )
        return 0  # exit clean: no token is a config issue, not an outage

    try:
        snapshot = _fetch_health(args.server_url, token)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        # Server unreachable. This is precisely when Vultr's own watchdog
        # is the right alert path (Mac-side has nothing to fix here), so
        # we just log and exit clean.
        print(
            f"[idle-watchdog] server unreachable ({exc}) — leaving "
            "this to Vultr's watchdog",
            file=sys.stderr,
        )
        return 0

    should_heal, reason = evaluate(
        snapshot, no_claim_minutes=args.no_claim_minutes
    )
    print(f"[idle-watchdog] {reason}", file=sys.stderr)
    trip_file = args.state_dir / "last-trip.json"
    if not should_heal:
        trip_file.unlink(missing_ok=True)
        return 0

    if not _confirm_repeat_trip(
        trip_file, snapshot, window_seconds=args.confirm_window_seconds
    ):
        print(
            "[idle-watchdog] first tick to trip — waiting for a second "
            "before healing",
            file=sys.stderr,
        )
        return 0

    stamp = args.state_dir / "last-heal.stamp"
    if _within_cooldown(stamp, args.cooldown_seconds):
        print(
            f"[idle-watchdog] within {args.cooldown_seconds}s cooldown — "
            "skipping heal this tick",
            file=sys.stderr,
        )
        return 0
    _stamp_now(stamp)

    incident = _write_incident(args.state_dir, snapshot, reason)
    print(
        f"[idle-watchdog] handing incident to self-heal agent ({incident})",
        file=sys.stderr,
    )
    heal_rc = _run_heal(
        incident_path=incident,
        repo=args.repo,
        log_dir=args.log_dir / "heal",
        timeout_seconds=args.heal_timeout,
    )

    if heal_rc == HEAL_EXIT_SUCCESS:
        print("[idle-watchdog] self-heal SUCCESS — no Telegram", file=sys.stderr)
        return 0
    if heal_rc in (HEAL_EXIT_ESCALATE, HEAL_EXIT_TIMEOUT, HEAL_EXIT_UNAVAILABLE):
        print(
            f"[idle-watchdog] self-heal escalated (rc={heal_rc}) — "
            "Telegram sent by heal CLI",
            file=sys.stderr,
        )
        return 0

    print(
        f"[idle-watchdog] heal CLI returned unexpected rc={heal_rc} — "
        "falling back to direct Telegram",
        file=sys.stderr,
    )
    _direct_telegram_fallback(repo=args.repo, reason=reason)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
