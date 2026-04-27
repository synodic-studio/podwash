"""Behavioural tests for src/alerting.py.

We stub urlopen so the tests are hermetic — Telegram is never called.
"""

from __future__ import annotations

import json

import pytest

from src import alerting


@pytest.fixture
def configured_env(monkeypatch, tmp_path):
    monkeypatch.setenv("ALERT_TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("ALERT_TELEGRAM_CHAT_ID", "-100123")
    monkeypatch.setenv("PODWASH_ALERT_STATE_DIR", str(tmp_path))
    # Force the module to re-resolve the rate dir.
    monkeypatch.setattr(alerting, "_RATE_DIR", tmp_path / "podwash-alerts")
    return tmp_path


@pytest.fixture
def fake_telegram(monkeypatch):
    """Replace urlopen with a recorder that returns a successful payload."""
    sent: list[dict] = []

    class _Resp:
        def __init__(self, body: bytes):
            self._body = body

        def read(self) -> bytes:
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake_urlopen(req, timeout: float = 0):  # noqa: ARG001
        # urllib.request.Request stores the URL-encoded body in req.data.
        from urllib.parse import parse_qs

        decoded = parse_qs(req.data.decode())
        sent.append({k: v[0] for k, v in decoded.items()})
        return _Resp(json.dumps({"ok": True, "result": {"message_id": 1}}).encode())

    monkeypatch.setattr(alerting.urllib.request, "urlopen", _fake_urlopen)
    return sent


def test_unconfigured_returns_false_and_prints_to_stderr(monkeypatch, capsys):
    monkeypatch.delenv("ALERT_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ALERT_TELEGRAM_CHAT_ID", raising=False)
    assert alerting.is_configured() is False

    ok = alerting.send_alert("worker", "thing broke", "p", "f", throttle_seconds=0)
    assert ok is False
    err = capsys.readouterr().err
    assert "🚨 [podwash/worker] thing broke" in err
    assert "Problem: p" in err
    assert "ALERT_TELEGRAM_BOT_TOKEN/CHAT_ID not set" in err


def test_format_includes_subsystem_kind_problem_fix_and_context():
    text = alerting._format(
        "worker",
        "boom",
        "p1",
        "f1",
        {"a": 1, "b": "two"},
    )
    assert text.splitlines()[0] == "🚨 [podwash/worker] boom"
    assert "Problem: p1" in text
    assert "Fix: f1" in text
    assert "  a: 1" in text
    assert "  b: two" in text


def test_send_when_configured_calls_telegram_once(configured_env, fake_telegram):
    ok = alerting.send_alert("worker", "first", "p", "f", throttle_seconds=0)
    assert ok is True
    assert len(fake_telegram) == 1
    payload = fake_telegram[0]
    assert payload["chat_id"] == "-100123"
    assert "first" in payload["text"]


def test_throttle_suppresses_duplicate_within_window(configured_env, fake_telegram):
    a = alerting.send_alert("worker", "same", "p", "f", throttle_seconds=3600)
    b = alerting.send_alert("worker", "same", "p", "f", throttle_seconds=3600)
    assert a is True
    assert b is False  # throttled
    assert len(fake_telegram) == 1


def test_throttle_zero_means_no_throttle(configured_env, fake_telegram):
    alerting.send_alert("w", "k", "p", "f", throttle_seconds=0)
    alerting.send_alert("w", "k", "p", "f", throttle_seconds=0)
    assert len(fake_telegram) == 2


def test_thread_id_included_when_set(monkeypatch, configured_env, fake_telegram):
    monkeypatch.setenv("ALERT_TELEGRAM_THREAD_ID", "42")
    alerting.send_alert("w", "k", "p", "f", throttle_seconds=0)
    assert fake_telegram[0]["message_thread_id"] == "42"


def test_throttle_marker_round_trip(monkeypatch, tmp_path):
    monkeypatch.setattr(alerting, "_RATE_DIR", tmp_path)
    assert alerting._throttled("w", "k", 60) is False
    assert alerting._throttled("w", "k", 60) is True
    # Different kind is independent.
    assert alerting._throttled("w", "other", 60) is False


def test_resolve_bot_token_prefers_env(monkeypatch):
    monkeypatch.setenv("ALERT_TELEGRAM_BOT_TOKEN", "from-env")
    assert alerting._resolve_bot_token() == "from-env"


def test_resolve_bot_token_falls_back_to_pass(monkeypatch):
    monkeypatch.delenv("ALERT_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setattr(alerting.shutil, "which", lambda name: "/usr/bin/pass")

    class _R:
        returncode = 0
        stdout = "from-pass\n"

    monkeypatch.setattr(alerting.subprocess, "run", lambda *a, **kw: _R())
    assert alerting._resolve_bot_token() == "from-pass"


def test_resolve_bot_token_returns_empty_when_pass_missing(monkeypatch):
    monkeypatch.delenv("ALERT_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setattr(alerting.shutil, "which", lambda name: None)
    assert alerting._resolve_bot_token() == ""


def test_cli_returns_1_when_unconfigured(monkeypatch):
    monkeypatch.delenv("ALERT_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ALERT_TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setattr(alerting.shutil, "which", lambda name: None)
    monkeypatch.setattr("sys.argv", ["podwash-alert-test"])
    assert alerting._cli() == 1


def test_cli_returns_0_when_send_succeeds(monkeypatch, configured_env, fake_telegram):
    monkeypatch.setattr("sys.argv", ["podwash-alert-test", "--note", "from-test"])
    rc = alerting._cli()
    assert rc == 0
    assert fake_telegram, "expected at least one Telegram send"
    assert "from-test" in fake_telegram[0]["text"]


def test_cli_returns_2_when_send_fails(monkeypatch, configured_env, fake_telegram):
    # Make urlopen raise to simulate API failure.
    def _boom(_req, timeout=0):
        raise ConnectionError("kapow")

    monkeypatch.setattr(alerting.urllib.request, "urlopen", _boom)
    monkeypatch.setattr("sys.argv", ["podwash-alert-test"])
    assert alerting._cli() == 2
