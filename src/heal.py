"""Self-heal via headless Claude Code.

When the worker has crashed enough times that the wrapper would
otherwise Telegram, we instead spawn `claude -p` non-interactively with
the failure context and let it try to fix the repo. Only if that fails
do we escalate to Telegram. Lesson: alerts are expensive (operator
attention); a Claude Code session is cheap.

Contract with the heal agent:
  - We pass it the incident as the prompt body.
  - It writes its verdict to the path we give in $PODWASH_HEAL_STATUS:
      first line is exactly "SUCCESS" or "ESCALATE"
      remaining lines are free-form notes (shown in the Telegram alert
      if we escalate)
  - If the file is missing or malformed when the timeout expires we treat
    it as ESCALATE with the timeout as the reason. Better to over-alert
    than to silently swallow a heal that lied about success.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

HEAL_TIMEOUT_SECONDS = int(os.environ.get("PODWASH_HEAL_TIMEOUT_SECONDS", "600"))
HEAL_POLL_SECONDS = float(os.environ.get("PODWASH_HEAL_POLL_SECONDS", "5"))
# Cap how many heal attempts we'll make per rolling 24h. If we hit this,
# something is flapping (real outage, broken upstream, recurring root
# cause) and we should hand it to a human instead of burning more
# Anthropic credits.
HEAL_MAX_ATTEMPTS_PER_DAY = int(os.environ.get("PODWASH_HEAL_MAX_PER_DAY", "6"))


def _record_heal_attempt(log_dir: Path) -> int:
    """Append now to the attempts log; return the count in last 24h."""
    log_dir.mkdir(parents=True, exist_ok=True)
    attempts = log_dir / "attempts.log"
    now = time.time()
    cutoff = now - 86400
    kept: list[float] = []
    if attempts.exists():
        for line in attempts.read_text().splitlines():
            try:
                ts = float(line.strip())
            except ValueError:
                continue
            if ts >= cutoff:
                kept.append(ts)
    kept.append(now)
    attempts.write_text("\n".join(f"{ts:.0f}" for ts in kept) + "\n")
    return len(kept)


def _attempts_in_last_day(log_dir: Path) -> int:
    """Return how many heal attempts are recorded in the last 24h."""
    attempts = log_dir / "attempts.log"
    if not attempts.exists():
        return 0
    cutoff = time.time() - 86400
    n = 0
    for line in attempts.read_text().splitlines():
        try:
            if float(line.strip()) >= cutoff:
                n += 1
        except ValueError:
            continue
    return n


@dataclass
class HealOutcome:
    status: str  # "SUCCESS" | "ESCALATE" | "TIMEOUT" | "UNAVAILABLE"
    notes: str
    transcript_path: Path | None

    @property
    def healed(self) -> bool:
        return self.status == "SUCCESS"


# Claude Code installs to ~/.local/bin which the launchd plist's PATH
# doesn't include. Search PATH first, then well-known fallbacks.
_CLAUDE_FALLBACK_PATHS = (
    Path.home() / ".local" / "bin" / "claude",
    Path("/opt/homebrew/bin/claude"),
    Path("/usr/local/bin/claude"),
)


def resolve_claude_bin() -> str | None:
    """Return absolute path to a runnable `claude`, or None."""
    on_path = shutil.which("claude")
    if on_path:
        return on_path
    for candidate in _CLAUDE_FALLBACK_PATHS:
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def claude_available() -> bool:
    return resolve_claude_bin() is not None


def _build_prompt(incident: str, repo: Path, status_file: Path) -> str:
    return (
        "You are a headless self-heal agent for the podwash project. "
        "The worker launchd job on this Mac is in a crash loop. Diagnose "
        "and fix the root cause if you can.\n\n"
        f"Repo: {repo}\n"
        f"Status file (you MUST write to this): {status_file}\n\n"
        "Incident report:\n"
        "----------------\n"
        f"{incident}\n"
        "----------------\n\n"
        "Common causes you can fix yourself:\n"
        "  - Missing worker extras: `uv sync --extra worker` in the repo\n"
        "  - Stale .venv: delete and re-create with `uv sync --extra worker`\n"
        "  - Network to the server is down (e.g. VPN/Tailscale dropped)\n"
        "  - Wrong server_url in config.yml after a server address change\n\n"
        "After attempting a fix, verify by running the preflight in isolation:\n"
        "  `cd " + str(repo) + " && uv run --extra worker python -c "
        "'from src.config import load_settings; from src.worker.preflight "
        "import run_preflight; import sys; sys.exit(0 if run_preflight("
        'load_settings(), "heal-verify") else 1)\'`\n\n'
        "Then write to the status file:\n"
        "  - First line: exactly `SUCCESS` or `ESCALATE`\n"
        "  - Subsequent lines: a short human-readable summary of what you "
        "did (or why you cannot fix it). This goes into the Telegram "
        "alert if you escalate.\n\n"
        "Write ESCALATE for things that need a human: revoked API keys, "
        "billing/quota issues, hardware problems, anything ambiguous. "
        "Better to escalate than to claim a fix that didn't take."
    )


def attempt_heal(
    incident: str,
    repo: Path,
    log_dir: Path,
    timeout_seconds: int = HEAL_TIMEOUT_SECONDS,
) -> HealOutcome:
    """Spawn headless Claude, poll the status file, return the outcome.

    The status file is created in a temp dir we clean up. The transcript
    of Claude's stdout is preserved in `log_dir` so a failed heal can be
    read after the fact.
    """
    if not claude_available():
        return HealOutcome(
            status="UNAVAILABLE",
            notes="`claude` CLI not on PATH; cannot self-heal.",
            transcript_path=None,
        )

    log_dir.mkdir(parents=True, exist_ok=True)
    transcript_path = log_dir / f"heal-{int(time.time())}.log"

    with tempfile.TemporaryDirectory(prefix="podwash-heal-") as tmpdir:
        status_file = Path(tmpdir) / "status"
        prompt = _build_prompt(incident, repo, status_file)

        env = dict(os.environ)
        env["PODWASH_HEAL_STATUS"] = str(status_file)

        # `claude -p` runs the agent non-interactively. We need
        # bypassPermissions because the heal agent must run Bash (uv
        # sync, tailscale up, etc.) without prompting — there's no human
        # to approve. --max-budget-usd caps a runaway agent's spend per
        # heal so a confused agent can't burn dollars in a loop.
        bin_path = resolve_claude_bin()
        if bin_path is None:
            return HealOutcome(
                status="UNAVAILABLE",
                notes="`claude` CLI vanished between availability check and spawn.",
                transcript_path=None,
            )
        max_usd = os.environ.get("PODWASH_HEAL_MAX_USD", "5")
        proc = subprocess.Popen(
            [
                bin_path,
                "-p",
                prompt,
                "--permission-mode",
                "bypassPermissions",
                "--dangerously-skip-permissions",
                "--max-budget-usd",
                max_usd,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=str(repo),
            env=env,
            text=True,
        )

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if status_file.exists():
                break
            if proc.poll() is not None:
                # Claude exited before writing status; give it a moment
                # for fs flush, then break either way.
                time.sleep(1)
                break
            time.sleep(HEAL_POLL_SECONDS)

        # Wrap up the subprocess.
        try:
            stdout, _ = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, _ = proc.communicate()
        try:
            transcript_path.write_text(stdout or "")
        except OSError:
            transcript_path = None  # type: ignore[assignment]

        if not status_file.exists():
            return HealOutcome(
                status="TIMEOUT",
                notes=(
                    f"Heal agent did not write a status within "
                    f"{timeout_seconds}s. See transcript."
                ),
                transcript_path=transcript_path,
            )

        raw = status_file.read_text().strip()

    first, _, rest = raw.partition("\n")
    first = first.strip().upper()
    notes = rest.strip()

    if first == "SUCCESS":
        return HealOutcome(
            status="SUCCESS", notes=notes, transcript_path=transcript_path
        )
    if first == "ESCALATE":
        return HealOutcome(
            status="ESCALATE",
            notes=notes or "Heal agent escalated without a reason.",
            transcript_path=transcript_path,
        )
    return HealOutcome(
        status="ESCALATE",
        notes=f"Malformed heal status (first line: {first!r}). Treating as escalate.",
        transcript_path=transcript_path,
    )


# CLI used by deploy/podwash-worker-wrapper.sh. The wrapper has the
# incident on stdin (or as a file). Exit codes:
#   0 = SUCCESS (heal worked, do not Telegram)
#   2 = ESCALATE (heal couldn't fix; wrapper should Telegram with notes)
#   3 = TIMEOUT (heal didn't finish in time; wrapper should Telegram)
#   4 = UNAVAILABLE (no `claude` CLI; wrapper should Telegram with raw incident)
def _cli() -> int:
    import argparse
    import sys
    from src.alerting import send_alert

    parser = argparse.ArgumentParser(prog="podwash-heal")
    parser.add_argument(
        "--incident-file",
        type=Path,
        required=True,
        help="Path to a file containing the incident report.",
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="Repo root the heal agent should operate in.",
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=Path.home() / "Library" / "Logs" / "podwash-worker" / "heal",
        help="Directory to keep heal-attempt transcripts in.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=HEAL_TIMEOUT_SECONDS,
        help="Seconds to wait for the heal agent before giving up.",
    )
    parser.add_argument(
        "--telegram-on-escalate",
        action="store_true",
        help="Send Telegram alert when escalating (instead of just exiting).",
    )
    parser.add_argument(
        "--max-per-day",
        type=int,
        default=HEAL_MAX_ATTEMPTS_PER_DAY,
        help="If this many heals already ran in 24h, escalate without retrying.",
    )
    args = parser.parse_args()

    incident = args.incident_file.read_text() if args.incident_file.exists() else ""
    if not incident.strip():
        print("[heal] no incident text — nothing to heal", file=sys.stderr)
        return 4

    # Daily-cap check happens BEFORE we record the attempt — otherwise
    # the cap becomes off-by-one with itself.
    already = _attempts_in_last_day(args.log_dir)
    if already >= args.max_per_day:
        msg = (
            f"Self-heal cap reached: {already} attempts in last 24h "
            f"(limit {args.max_per_day}). Skipping headless agent and "
            "escalating directly — something is genuinely flapping and "
            "needs human eyes."
        )
        print(f"[heal] {msg}", file=sys.stderr)
        if args.telegram_on_escalate:
            send_alert(
                subsystem="worker-runtime",
                kind="self-heal capped",
                problem=msg,
                fix=(
                    "Read the recent transcripts in "
                    f"{args.log_dir} to see what the agent has been "
                    "trying. The underlying problem isn't being fixed "
                    "by automation."
                ),
                context={"incident_excerpt": incident[:1000]},
            )
        return 2

    _record_heal_attempt(args.log_dir)

    outcome = attempt_heal(
        incident=incident,
        repo=args.repo,
        log_dir=args.log_dir,
        timeout_seconds=args.timeout,
    )
    print(f"[heal] outcome={outcome.status}", file=sys.stderr)
    if outcome.notes:
        print(f"[heal] notes: {outcome.notes}", file=sys.stderr)
    if outcome.transcript_path:
        print(f"[heal] transcript: {outcome.transcript_path}", file=sys.stderr)

    code_map = {"SUCCESS": 0, "ESCALATE": 2, "TIMEOUT": 3, "UNAVAILABLE": 4}
    exit_code = code_map.get(outcome.status, 2)

    if exit_code != 0 and args.telegram_on_escalate:
        send_alert(
            subsystem="worker-runtime",
            kind=f"self-heal {outcome.status.lower()}",
            problem=(
                "podwash-worker is in a crash loop and the headless "
                f"self-heal attempt returned {outcome.status}."
            ),
            fix=(
                outcome.notes
                + (
                    f"\nFull heal transcript: {outcome.transcript_path}"
                    if outcome.transcript_path
                    else ""
                )
                + "\nIncident:\n"
                + incident
            ),
        )

    return exit_code


if __name__ == "__main__":
    raise SystemExit(_cli())
