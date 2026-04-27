"""Tests for the self-heal helper.

We never actually spawn `claude -p`. Instead we replace
subprocess.Popen with a fake that simulates the agent: it writes the
status file we configure, then exits.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from src import heal


class _FakePopen:
    """Stand-in for subprocess.Popen. Writes a configured status file
    after a short delay (so the polling loop in attempt_heal exercises
    its wait path) then 'exits' with the configured stdout."""

    def __init__(
        self,
        *,
        status_text: str | None,
        stdout_text: str = "fake stdout",
        delay: float = 0.05,
        env: dict | None = None,
    ):
        self._status_text = status_text
        self._stdout_text = stdout_text
        self._delay = delay
        self._env = env or {}
        self._exited = False
        self._t: threading.Thread | None = None

        if status_text is not None and "PODWASH_HEAL_STATUS" in self._env:
            target = Path(self._env["PODWASH_HEAL_STATUS"])

            def _write_after_delay():
                time.sleep(self._delay)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(self._status_text)
                self._exited = True

            self._t = threading.Thread(target=_write_after_delay, daemon=True)
            self._t.start()
        else:
            # No status file → simulate immediate exit (timeout path).
            self._exited = True

    def poll(self):
        return 0 if self._exited else None

    def communicate(self, timeout=None):
        if self._t is not None:
            self._t.join(timeout=timeout)
        return self._stdout_text, ""

    def kill(self):
        self._exited = True


@pytest.fixture
def patched_subprocess(monkeypatch):
    """Returns a function the test calls to install a fake Popen."""
    holder: dict = {}

    # Speed up polling — production uses 5s but tests want sub-second.
    monkeypatch.setattr(heal, "HEAL_POLL_SECONDS", 0.02)

    def install(*, status_text, stdout_text="x", delay=0.02):
        def _factory(_args, **kwargs):
            holder["env"] = kwargs.get("env", {})
            holder["cwd"] = kwargs.get("cwd")
            return _FakePopen(
                status_text=status_text,
                stdout_text=stdout_text,
                delay=delay,
                env=kwargs.get("env"),
            )

        monkeypatch.setattr(heal.subprocess, "Popen", _factory)
        # Force claude_available() to True regardless of host PATH.
        monkeypatch.setattr(heal, "claude_available", lambda: True)

    return install, holder


def test_unavailable_when_claude_cli_not_on_path(monkeypatch, tmp_path):
    monkeypatch.setattr(heal, "claude_available", lambda: False)
    out = heal.attempt_heal(incident="boom", repo=tmp_path, log_dir=tmp_path / "logs")
    assert out.status == "UNAVAILABLE"
    assert out.healed is False


def test_success_status_returns_healed(patched_subprocess, tmp_path):
    install, _ = patched_subprocess
    install(status_text="SUCCESS\nran uv sync --extra worker")
    out = heal.attempt_heal(
        incident="missing module",
        repo=tmp_path,
        log_dir=tmp_path / "logs",
        timeout_seconds=5,
    )
    assert out.status == "SUCCESS"
    assert out.healed is True
    assert "uv sync" in out.notes
    assert out.transcript_path is not None
    assert out.transcript_path.exists()


def test_escalate_status_with_notes_preserved(patched_subprocess, tmp_path):
    install, _ = patched_subprocess
    install(status_text="ESCALATE\nanthropic key revoked, needs rotation")
    out = heal.attempt_heal(
        incident="auth fail",
        repo=tmp_path,
        log_dir=tmp_path / "logs",
        timeout_seconds=5,
    )
    assert out.status == "ESCALATE"
    assert out.healed is False
    assert "key revoked" in out.notes


def test_malformed_first_line_treated_as_escalate(patched_subprocess, tmp_path):
    install, _ = patched_subprocess
    install(status_text="MAYBE\nwho knows")
    out = heal.attempt_heal(
        incident="?", repo=tmp_path, log_dir=tmp_path / "logs", timeout_seconds=5
    )
    assert out.status == "ESCALATE"
    assert "Malformed" in out.notes


def test_timeout_when_status_file_never_written(
    patched_subprocess, tmp_path, monkeypatch
):
    install, _ = patched_subprocess
    install(status_text=None)  # FakePopen will not write a status file
    # Speed the polling loop up so the test stays under a second.
    monkeypatch.setattr(heal, "HEAL_POLL_SECONDS", 0.05)
    out = heal.attempt_heal(
        incident="?", repo=tmp_path, log_dir=tmp_path / "logs", timeout_seconds=1
    )
    assert out.status == "TIMEOUT"
    assert "did not write a status" in out.notes


def test_status_file_path_passed_via_env(patched_subprocess, tmp_path):
    install, holder = patched_subprocess
    install(status_text="SUCCESS")
    heal.attempt_heal(
        incident="x", repo=tmp_path, log_dir=tmp_path / "logs", timeout_seconds=5
    )
    assert "PODWASH_HEAL_STATUS" in holder["env"]
    # And the env var path is what the agent was told to write to.
    assert holder["env"]["PODWASH_HEAL_STATUS"].endswith("status")


def test_cli_returns_zero_on_success(monkeypatch, tmp_path, capsys):
    incident = tmp_path / "inc.txt"
    incident.write_text("crash burst x3")

    fake_outcome = heal.HealOutcome(
        status="SUCCESS", notes="ran uv sync", transcript_path=None
    )
    monkeypatch.setattr(heal, "attempt_heal", lambda **kw: fake_outcome)
    monkeypatch.setattr(
        "sys.argv",
        [
            "podwash-heal",
            "--incident-file",
            str(incident),
            "--repo",
            str(tmp_path),
            "--log-dir",
            str(tmp_path / "heal"),
        ],
    )
    rc = heal._cli()
    assert rc == 0


def test_cli_returns_2_and_telegrams_on_escalate(monkeypatch, tmp_path):
    incident = tmp_path / "inc.txt"
    incident.write_text("crash burst x3")

    sent: list[dict] = []

    fake_outcome = heal.HealOutcome(
        status="ESCALATE", notes="needs human", transcript_path=tmp_path / "t.log"
    )
    monkeypatch.setattr(heal, "attempt_heal", lambda **kw: fake_outcome)
    monkeypatch.setattr(
        "src.alerting.send_alert",
        lambda **kw: sent.append(kw) or True,
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "podwash-heal",
            "--incident-file",
            str(incident),
            "--repo",
            str(tmp_path),
            "--log-dir",
            str(tmp_path / "heal"),
            "--telegram-on-escalate",
        ],
    )
    rc = heal._cli()
    assert rc == 2
    assert sent and sent[0]["subsystem"] == "worker-runtime"
    assert "self-heal escalate" in sent[0]["kind"]
    # Heal notes + incident text both surface in the alert fix.
    assert "needs human" in sent[0]["fix"]
    assert "crash burst" in sent[0]["fix"]


def test_cli_returns_3_on_timeout(monkeypatch, tmp_path):
    incident = tmp_path / "inc.txt"
    incident.write_text("crash burst x3")
    monkeypatch.setattr(
        heal,
        "attempt_heal",
        lambda **kw: heal.HealOutcome(
            status="TIMEOUT", notes="never wrote status", transcript_path=None
        ),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "podwash-heal",
            "--incident-file",
            str(incident),
            "--repo",
            str(tmp_path),
            "--log-dir",
            str(tmp_path / "heal"),
        ],
    )
    rc = heal._cli()
    assert rc == 3


def test_cli_returns_4_on_empty_incident(monkeypatch, tmp_path):
    incident = tmp_path / "inc.txt"
    incident.write_text("")
    monkeypatch.setattr(
        "sys.argv",
        ["podwash-heal", "--incident-file", str(incident), "--repo", str(tmp_path)],
    )
    rc = heal._cli()
    assert rc == 4


def test_resolve_claude_bin_finds_local_bin_when_path_misses(monkeypatch, tmp_path):
    # Simulate a launchd-style PATH that doesn't include ~/.local/bin.
    monkeypatch.setattr(heal.shutil, "which", lambda _name: None)
    fake_local = tmp_path / "claude"
    fake_local.write_text("#!/bin/sh\nexit 0\n")
    fake_local.chmod(0o755)
    monkeypatch.setattr(heal, "_CLAUDE_FALLBACK_PATHS", (fake_local,))
    assert heal.resolve_claude_bin() == str(fake_local)


def test_resolve_claude_bin_returns_none_when_nothing_found(monkeypatch, tmp_path):
    monkeypatch.setattr(heal.shutil, "which", lambda _name: None)
    monkeypatch.setattr(heal, "_CLAUDE_FALLBACK_PATHS", (tmp_path / "nope",))
    assert heal.resolve_claude_bin() is None


def test_attempt_heal_passes_bypass_permissions_and_budget(
    patched_subprocess, tmp_path
):
    """Regression test: the heal agent MUST run with bypassPermissions
    or every Bash call will hang waiting for approval."""
    install, holder = patched_subprocess
    install(status_text="SUCCESS")
    # Capture the args the fake Popen received.
    captured: dict = {}
    real_popen_factory = heal.subprocess.Popen

    def _capturing_factory(args, **kwargs):
        captured["args"] = args
        return real_popen_factory(args, **kwargs)

    import builtins  # noqa: F401  (silence unused-import lint)

    heal.subprocess.Popen = _capturing_factory
    try:
        heal.attempt_heal(
            incident="boom", repo=tmp_path, log_dir=tmp_path / "logs", timeout_seconds=5
        )
    finally:
        heal.subprocess.Popen = real_popen_factory
    args = captured["args"]
    # Order doesn't matter, presence does.
    assert "--permission-mode" in args
    assert "bypassPermissions" in args
    assert "--dangerously-skip-permissions" in args
    assert "--max-budget-usd" in args


def test_record_and_count_heal_attempts(tmp_path):
    log_dir = tmp_path / "heal"
    assert heal._attempts_in_last_day(log_dir) == 0
    heal._record_heal_attempt(log_dir)
    heal._record_heal_attempt(log_dir)
    assert heal._attempts_in_last_day(log_dir) == 2


def test_attempts_log_drops_entries_older_than_24h(tmp_path):
    log_dir = tmp_path / "heal"
    log_dir.mkdir()
    attempts = log_dir / "attempts.log"
    # Two stale entries (2 days ago) + one fresh.
    import time as _time

    stale = _time.time() - 2 * 86400
    fresh = _time.time() - 60
    attempts.write_text(f"{stale:.0f}\n{stale:.0f}\n{fresh:.0f}\n")
    assert heal._attempts_in_last_day(log_dir) == 1


def test_cli_caps_at_daily_limit_and_escalates_directly(monkeypatch, tmp_path):
    incident = tmp_path / "inc.txt"
    incident.write_text("crash burst x3")
    log_dir = tmp_path / "heal"
    # Pre-seed the attempt log past the cap.
    log_dir.mkdir()
    import time as _time

    now = _time.time()
    (log_dir / "attempts.log").write_text(
        "\n".join(f"{now:.0f}" for _ in range(6)) + "\n"
    )

    sent: list[dict] = []
    monkeypatch.setattr("src.alerting.send_alert", lambda **kw: sent.append(kw) or True)
    # If we accidentally call attempt_heal we should fail loudly.
    monkeypatch.setattr(
        heal,
        "attempt_heal",
        lambda **_: pytest.fail("attempt_heal must not be called past the cap"),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "podwash-heal",
            "--incident-file",
            str(incident),
            "--repo",
            str(tmp_path),
            "--log-dir",
            str(log_dir),
            "--max-per-day",
            "6",
            "--telegram-on-escalate",
        ],
    )
    rc = heal._cli()
    assert rc == 2
    assert sent and sent[0]["kind"] == "self-heal capped"
    assert "6 attempts" in sent[0]["problem"]
