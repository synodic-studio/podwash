"""Telegram alerting for self-healing automation.

Used by the worker (Mac) and the server-side watchdog (Vultr) to surface
operational failures with structured Problem/Fix context — anything that
silently fails here cost us 3 days of dead processing in Apr 2026.

Configured via env vars (set in `.env` on each host):
    ALERT_TELEGRAM_BOT_TOKEN   bot API token
    ALERT_TELEGRAM_CHAT_ID     target chat (e.g. -100... for supergroups)
    ALERT_TELEGRAM_THREAD_ID   optional thread id within the chat

If unconfigured, send_alert() prints to stderr and returns False — the
caller can then decide whether to also exit non-zero. is_configured()
lets startup verify wiring before we rely on it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

_RATE_DIR = Path(os.environ.get("PODWASH_ALERT_STATE_DIR", "/tmp")) / "podwash-alerts"
_DEFAULT_THROTTLE_SECONDS = 3600  # one alert per (subsystem, kind) per hour


def _resolve_bot_token() -> str:
    """Bot token via env, else `pass show telegram-bot-token` if available.

    Hosts without `pass` (e.g. a Linux VPS) set ALERT_TELEGRAM_BOT_TOKEN
    directly in their `.env`. This indirection keeps launchd plists on a
    Mac free of plaintext secrets.
    """
    token = os.environ.get("ALERT_TELEGRAM_BOT_TOKEN", "")
    if token:
        return token
    if shutil.which("pass"):
        try:
            r = subprocess.run(
                ["pass", "show", "telegram-bot-token"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if r.returncode == 0:
                return r.stdout.strip()
        except Exception:
            pass
    return ""


def is_configured() -> bool:
    """True iff a bot token can be resolved AND a chat id is set."""
    return bool(_resolve_bot_token()) and bool(os.environ.get("ALERT_TELEGRAM_CHAT_ID"))


def _format(
    subsystem: str,
    kind: str,
    problem: str,
    fix: str,
    context: dict[str, Any] | None,
) -> str:
    lines = [
        f"🚨 [podwash/{subsystem}] {kind}",
        f"Problem: {problem}",
        f"Fix: {fix}",
    ]
    if context:
        lines.append("Context:")
        for k, v in context.items():
            lines.append(f"  {k}: {v}")
    return "\n".join(lines)


def _throttled(subsystem: str, kind: str, throttle_seconds: int) -> bool:
    """Return True if we've sent (subsystem, kind) within throttle_seconds."""
    if throttle_seconds <= 0:
        return False
    _RATE_DIR.mkdir(parents=True, exist_ok=True)
    safe_kind = "".join(c if c.isalnum() else "_" for c in kind)
    marker = _RATE_DIR / f"{subsystem}__{safe_kind}.last"
    now = time.time()
    if marker.exists():
        try:
            last = float(marker.read_text().strip())
        except ValueError:
            last = 0.0
        if now - last < throttle_seconds:
            return True
    marker.write_text(str(now))
    return False


def send_alert(
    subsystem: str,
    kind: str,
    problem: str,
    fix: str,
    context: dict[str, Any] | None = None,
    throttle_seconds: int = _DEFAULT_THROTTLE_SECONDS,
) -> bool:
    """Send a structured operational alert to Telegram.

    Returns True if the message was actually delivered. On any failure
    (no config, throttled, network error) the same message is also printed
    to stderr so it's never lost.
    """
    text = _format(subsystem, kind, problem, fix, context)
    print(text, file=sys.stderr, flush=True)

    if not is_configured():
        print(
            "[alerting] ALERT_TELEGRAM_BOT_TOKEN/CHAT_ID not set — "
            "alert printed to stderr only",
            file=sys.stderr,
        )
        return False

    if _throttled(subsystem, kind, throttle_seconds):
        return False

    token = _resolve_bot_token()
    chat_id = os.environ["ALERT_TELEGRAM_CHAT_ID"]
    thread_id = os.environ.get("ALERT_TELEGRAM_THREAD_ID", "")
    payload = {"chat_id": chat_id, "text": text}
    if thread_id:
        payload["message_thread_id"] = thread_id

    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=urllib.parse.urlencode(payload).encode(),
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8", errors="replace"))
        if not body.get("ok"):
            print(
                f"[alerting] Telegram rejected alert: {body.get('description')}",
                file=sys.stderr,
            )
            return False
        return True
    except Exception as exc:
        print(f"[alerting] Telegram send failed: {exc}", file=sys.stderr)
        return False


def _cli() -> int:
    """`python -m src.alerting` — verify the Telegram pipe is intact.

    Sends one clearly-marked test alert with throttling disabled, so the
    operator can confirm bot token + chat + thread are all wired without
    waiting for a real failure.
    """
    import argparse
    import platform
    import socket

    parser = argparse.ArgumentParser(prog="podwash-alert-test")
    parser.add_argument(
        "--note",
        default="manual wiring check",
        help="Free-form text included in the test alert.",
    )
    args = parser.parse_args()

    if not is_configured():
        print(
            "[alerting] Not configured. Need ALERT_TELEGRAM_CHAT_ID set "
            "and either ALERT_TELEGRAM_BOT_TOKEN env or "
            "`pass show telegram-bot-token` available.",
            file=sys.stderr,
        )
        return 1

    ok = send_alert(
        subsystem="self-test",
        kind="ping",
        problem=f"Test alert — wiring check from {platform.node()}.",
        fix="No action needed. If you see this, alerts work.",
        context={
            "host": socket.gethostname(),
            "note": args.note,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
        throttle_seconds=0,
    )
    if ok:
        print("[alerting] sent — check Telegram.")
        return 0
    print(
        "[alerting] send returned False — check stderr above for the reason.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli())
