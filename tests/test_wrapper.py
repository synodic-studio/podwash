"""Tests for src/worker/wrapper.py — the launchd entrypoint that
escalates worker crash bursts to the headless heal agent.

Bash version of this was untestable; Python port lets us exercise the
crash-counting math, threshold detection, and heal exit-code branching
without spawning real workers."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.worker import wrapper


@pytest.fixture
def env(tmp_path):
    """A repo/log/state layout the wrapper can write to."""
    return {
        "repo": tmp_path / "repo",
        "log": tmp_path / "logs",
        "state": tmp_path / "state",
    }


@pytest.fixture(autouse=True)
def _isolate_paths(env, monkeypatch):
    for p in env.values():
        p.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("REPO_DIR", str(env["repo"]))
    monkeypatch.setenv("LOG_DIR", str(env["log"]))
    monkeypatch.setenv("STATE_DIR", str(env["state"]))
    monkeypatch.setenv("UV_BIN", "/usr/bin/true")
    yield


def test_trim_drops_entries_outside_window(tmp_path):
    log = tmp_path / "crashes.log"
    log.write_text("100\n200\n900\n")
    # window=300, now=1000 → cutoff=700, only 900 + new entry survive
    n = wrapper._trim_crash_log(log, 300, 1000.0)
    assert n == 2
    assert log.read_text().splitlines() == ["900", "1000"]


def test_trim_handles_garbage_lines(tmp_path):
    log = tmp_path / "crashes.log"
    log.write_text("not-a-number\n\n100\n")
    n = wrapper._trim_crash_log(log, 10000, 200.0)
    assert n == 2  # the valid 100 + the new 200


def test_benign_exit_codes_propagate_unchanged(monkeypatch, env):
    monkeypatch.setattr(wrapper, "_run_worker", lambda *a, **kw: 130)
    monkeypatch.setattr(
        wrapper,
        "_run_heal",
        lambda **_: pytest.fail("heal must not run on benign exit"),
    )
    rc = wrapper.main([])
    assert rc == 130


def test_single_crash_does_not_trigger_heal(monkeypatch, env):
    monkeypatch.setattr(wrapper, "_run_worker", lambda *a, **kw: 1)
    monkeypatch.setattr(
        wrapper, "_run_heal", lambda **_: pytest.fail("heal must not run yet")
    )
    rc = wrapper.main(["--max-crashes", "3", "--window-seconds", "600"])
    assert rc == 1


def test_threshold_crashes_trigger_heal(monkeypatch, env):
    monkeypatch.setattr(wrapper, "_run_worker", lambda *a, **kw: 1)
    heal_calls: list[dict] = []

    def _fake_heal(**kw):
        heal_calls.append(kw)
        return wrapper.HEAL_EXIT_SUCCESS

    monkeypatch.setattr(wrapper, "_run_heal", _fake_heal)

    # Three crashes inside the window.
    for _ in range(3):
        wrapper.main(["--max-crashes", "3", "--window-seconds", "600"])
    assert len(heal_calls) == 1
    # The incident file actually got created and contains the worker info.
    incident_path = heal_calls[0]["incident_path"]
    assert incident_path.exists()
    body = incident_path.read_text()
    assert "podwash-worker crash burst" in body
    assert "exit_code: 1" in body


def test_heal_success_clears_counter_and_returns_zero(monkeypatch, env):
    monkeypatch.setattr(wrapper, "_run_worker", lambda *a, **kw: 1)
    monkeypatch.setattr(wrapper, "_run_heal", lambda **_: wrapper.HEAL_EXIT_SUCCESS)

    for _ in range(2):
        wrapper.main(["--max-crashes", "3"])
    rc = wrapper.main(["--max-crashes", "3"])
    assert rc == 0  # heal success → exit clean so launchd restarts immediately
    assert (env["state"] / "crashes.log").read_text() == ""


@pytest.mark.parametrize(
    "heal_rc",
    [
        wrapper.HEAL_EXIT_ESCALATE,
        wrapper.HEAL_EXIT_TIMEOUT,
        wrapper.HEAL_EXIT_UNAVAILABLE,
    ],
)
def test_heal_escalation_sleeps_and_propagates_exit(monkeypatch, env, heal_rc):
    monkeypatch.setattr(wrapper, "_run_worker", lambda *a, **kw: 7)
    monkeypatch.setattr(wrapper, "_run_heal", lambda **_: heal_rc)
    sleeps: list[int] = []
    monkeypatch.setattr(wrapper.time, "sleep", lambda s: sleeps.append(s))
    fallbacks: list[dict] = []
    monkeypatch.setattr(
        wrapper, "_direct_telegram_fallback", lambda **kw: fallbacks.append(kw)
    )

    for _ in range(3):
        rc = wrapper.main(["--max-crashes", "3", "--sleep-after-burst", "42"])
    assert rc == 7  # original worker exit propagates
    assert sleeps == [42]  # cooldown happened exactly once
    assert fallbacks == []  # known escalation; no fallback fired
    assert (env["state"] / "crashes.log").read_text() == ""


def test_unknown_heal_exit_triggers_direct_fallback(monkeypatch, env):
    monkeypatch.setattr(wrapper, "_run_worker", lambda *a, **kw: 9)
    monkeypatch.setattr(wrapper, "_run_heal", lambda **_: 99)
    monkeypatch.setattr(wrapper.time, "sleep", lambda s: None)
    fallbacks: list[dict] = []
    monkeypatch.setattr(
        wrapper, "_direct_telegram_fallback", lambda **kw: fallbacks.append(kw)
    )

    for _ in range(3):
        wrapper.main(["--max-crashes", "3"])
    assert len(fallbacks) == 1
    assert fallbacks[0]["exit_code"] == 9
    assert fallbacks[0]["crashes"] == 3


def test_uv_missing_returns_1(monkeypatch, env):
    monkeypatch.delenv("UV_BIN", raising=False)
    monkeypatch.setattr(wrapper.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        wrapper, "_run_worker", lambda *a, **kw: pytest.fail("must not run worker")
    )
    rc = wrapper.main([])
    assert rc == 1


def test_incident_includes_stderr_tail(tmp_path):
    err = tmp_path / "err.log"
    err.write_text("line one\nline two\nlast line of stderr\n")
    incident = wrapper._write_incident(
        state_dir=tmp_path,
        repo=tmp_path,
        err_log=err,
        exit_code=1,
        crashes_in_window=3,
        window_seconds=600,
        now=1000.0,
    )
    body = incident.read_text()
    assert "last line of stderr" in body
    assert "exit_code: 1" in body
    assert "crashes_in_window: 3" in body


def test_runs_with_system_python_only_imports(tmp_path):
    """Sanity check: wrapper.py imports only stdlib so it can run under
    /usr/bin/python3 with no project venv."""
    src = Path("src/worker/wrapper.py").read_text()
    # The wrapper itself imports nothing from the project at module
    # scope. Lazy imports of src.alerting are fine (used only in the
    # fallback path).
    forbidden = (
        "import anthropic",
        "import faster_whisper",
        "from anthropic",
        "from faster_whisper",
        "import yaml",
        "import pydantic",
    )
    for needle in forbidden:
        assert needle not in src, f"wrapper must stay stdlib-only ({needle})"
