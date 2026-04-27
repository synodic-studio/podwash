"""launchd entrypoint that runs the worker and escalates crash bursts.

Wraps `uv run --extra worker podwash-worker` so that:

  1. uv re-syncs deps on every boot (prevents the Apr 2026 silent
     ModuleNotFoundError class).
  2. After MAX_CRASHES crashes inside WINDOW_SECONDS, we don't Telegram
     the operator — we hand the incident to a headless `claude -p`
     self-heal agent (via `python -m src.heal`). Only
     ESCALATE/TIMEOUT/UNAVAILABLE results actually Telegram.
  3. After any escalation we sleep SLEEP_AFTER_BURST_SECONDS so a real
     outage doesn't spam the operator once a minute.

This module is intentionally stdlib-only. It runs under the system
Python (/usr/bin/python3) so a broken project venv can't keep the
wrapper itself from booting — the whole point is to detect and recover
from a broken venv.

launchd's KeepAlive still does the actual restart loop. This module is
the loud-failure layer on top of it.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Exit codes that mean "stopped intentionally" — not a crash.
_BENIGN_EXIT_CODES = {0, 130, 143}

# Heal CLI exit codes (defined alongside src.heal._cli for the source
# of truth — kept literal here so the wrapper doesn't have to import
# the project on every restart, even when imports may be broken).
HEAL_EXIT_SUCCESS = 0
HEAL_EXIT_ESCALATE = 2
HEAL_EXIT_TIMEOUT = 3
HEAL_EXIT_UNAVAILABLE = 4


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _trim_crash_log(crash_log: Path, window_seconds: int, now: float) -> int:
    """Append `now` to the crash log, drop entries older than the window,
    return the resulting count."""
    crash_log.parent.mkdir(parents=True, exist_ok=True)
    cutoff = now - window_seconds
    kept: list[float] = []
    if crash_log.exists():
        for line in crash_log.read_text().splitlines():
            try:
                ts = float(line.strip().split()[0])
            except (ValueError, IndexError):
                continue
            if ts >= cutoff:
                kept.append(ts)
    kept.append(now)
    crash_log.write_text("\n".join(f"{ts:.0f}" for ts in kept) + "\n")
    return len(kept)


def _read_stderr_tail(err_log: Path, max_bytes: int = 6000) -> str:
    if not err_log.exists():
        return ""
    try:
        data = err_log.read_bytes()
    except OSError:
        return ""
    return data[-max_bytes:].decode("utf-8", errors="replace")


def _write_incident(
    *,
    state_dir: Path,
    repo: Path,
    err_log: Path,
    exit_code: int,
    crashes_in_window: int,
    window_seconds: int,
    now: float,
) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / f"incident-{int(now)}.txt"
    body = (
        "podwash-worker crash burst\n"
        f"exit_code: {exit_code}\n"
        f"crashes_in_window: {crashes_in_window} (window={window_seconds}s)\n"
        f"repo: {repo}\n"
        f"timestamp: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now))}\n"
        "\n"
        "stderr tail:\n"
        f"{_read_stderr_tail(err_log)}"
    )
    path.write_text(body)
    return path


def _run_worker(uv_bin: str, repo: Path) -> int:
    """Spawn the worker. Inherits stdio so launchd's StandardOutPath /
    StandardErrorPath capture preflight + worker logs as before."""
    proc = subprocess.run(
        [uv_bin, "run", "--extra", "worker", "podwash-worker"],
        cwd=str(repo),
    )
    return proc.returncode


def _run_heal(
    *,
    incident_path: Path,
    repo: Path,
    log_dir: Path,
    timeout_seconds: int,
) -> int:
    """Invoke `python -m src.heal` with this same interpreter.

    Returns the heal CLI's exit code (HEAL_EXIT_*).
    """
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


def _direct_telegram_fallback(
    *, repo: Path, exit_code: int, crashes: int, err_log: Path, cooldown: int
) -> None:
    """Last resort: the heal CLI itself failed. Try to import alerting
    and send a single Telegram. Never raises — even alerting failure
    can't take down the wrapper."""
    sys.path.insert(0, str(repo))
    try:
        from src.alerting import send_alert
    except Exception as exc:
        print(
            f"[wrapper] could not import alerting for fallback: {exc}",
            file=sys.stderr,
        )
        return
    try:
        send_alert(
            subsystem="worker-runtime",
            kind="self-heal infrastructure failure",
            problem=(
                f"podwash-worker crashed {crashes}x and the self-heal "
                f"CLI itself returned an unexpected exit code "
                f"({exit_code}). Wrapper is sleeping {cooldown}s."
            ),
            fix=(
                f"Read the heal logs in {err_log.parent}/heal/ and the "
                f"worker stderr at {err_log}. Likely the heal module "
                "itself is broken or python -m src.heal cannot import."
            ),
            throttle_seconds=cooldown,
        )
    except Exception as exc:
        print(f"[wrapper] alerting fallback raised: {exc}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="podwash-worker-wrapper")
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
        default=Path(os.environ.get("STATE_DIR", "/tmp/podwash-worker-wrapper")),
    )
    parser.add_argument("--uv-bin", default=os.environ.get("UV_BIN") or "")
    parser.add_argument("--max-crashes", type=int, default=_env_int("MAX_CRASHES", 3))
    parser.add_argument(
        "--window-seconds", type=int, default=_env_int("WINDOW_SECONDS", 600)
    )
    parser.add_argument(
        "--sleep-after-burst",
        type=int,
        default=_env_int("SLEEP_AFTER_BURST_SECONDS", 3600),
    )
    parser.add_argument(
        "--heal-timeout",
        type=int,
        default=_env_int("HEAL_TIMEOUT_SECONDS", 600),
    )
    args = parser.parse_args(argv)

    repo: Path = args.repo
    log_dir: Path = args.log_dir
    state_dir: Path = args.state_dir
    uv_bin = args.uv_bin or shutil.which("uv") or ""

    if not uv_bin:
        print("[wrapper] uv not found in PATH or UV_BIN", file=sys.stderr)
        return 1

    log_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    crash_log = state_dir / "crashes.log"
    err_log = log_dir / "podwash-worker.err"

    exit_code = _run_worker(uv_bin, repo)
    if exit_code in _BENIGN_EXIT_CODES:
        return exit_code

    now = time.time()
    crash_count = _trim_crash_log(crash_log, args.window_seconds, now)
    print(
        f"[wrapper] worker exited {exit_code} — "
        f"{crash_count} crash(es) in last {args.window_seconds}s",
        file=sys.stderr,
    )

    if crash_count < args.max_crashes:
        return exit_code

    incident = _write_incident(
        state_dir=state_dir,
        repo=repo,
        err_log=err_log,
        exit_code=exit_code,
        crashes_in_window=crash_count,
        window_seconds=args.window_seconds,
        now=now,
    )
    print(
        f"[wrapper] handing incident to self-heal agent ({incident})", file=sys.stderr
    )
    heal_rc = _run_heal(
        incident_path=incident,
        repo=repo,
        log_dir=log_dir / "heal",
        timeout_seconds=args.heal_timeout,
    )

    if heal_rc == HEAL_EXIT_SUCCESS:
        print(
            "[wrapper] self-heal SUCCESS — clearing counter, no Telegram",
            file=sys.stderr,
        )
        crash_log.write_text("")
        return 0  # let launchd restart immediately to verify

    if heal_rc in (HEAL_EXIT_ESCALATE, HEAL_EXIT_TIMEOUT, HEAL_EXIT_UNAVAILABLE):
        print(
            f"[wrapper] self-heal escalated (rc={heal_rc}) — Telegram sent by heal CLI",
            file=sys.stderr,
        )
        crash_log.write_text("")
        print(
            f"[wrapper] sleeping {args.sleep_after_burst}s to avoid "
            "retrying during outage",
            file=sys.stderr,
        )
        time.sleep(args.sleep_after_burst)
        return exit_code

    # Unknown heal exit code — alert ourselves so the failure isn't silent.
    print(
        f"[wrapper] heal CLI returned unexpected rc={heal_rc} — "
        "falling back to direct Telegram",
        file=sys.stderr,
    )
    _direct_telegram_fallback(
        repo=repo,
        exit_code=exit_code,
        crashes=crash_count,
        err_log=err_log,
        cooldown=args.sleep_after_burst,
    )
    crash_log.write_text("")
    time.sleep(args.sleep_after_burst)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
