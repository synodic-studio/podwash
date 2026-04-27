"""Tests for src/worker/preflight.py.

We exercise each check individually with controlled inputs, then verify
that run_preflight emits the structured PREFLIGHT_FAILURE block on the
first failure and never sends Telegram (preflight is supposed to be
the silent-failure-detector; the wrapper handles escalation).
"""

from __future__ import annotations

from contextlib import contextmanager


from src.config import Settings
from src.worker import preflight


def _settings(token="t", api_key="k", server_url="http://example.test:0"):
    s = Settings()
    s.worker.token = token
    s.worker.server_url = server_url
    s.anthropic_api_key = api_key
    return s


def test_check_secrets_passes_when_both_set():
    ok, problem, fix = preflight._check_secrets(_settings())
    assert ok is True
    assert problem == ""
    assert fix == ""


def test_check_secrets_lists_each_missing_secret():
    s = _settings(token="", api_key="")
    ok, problem, fix = preflight._check_secrets(s)
    assert ok is False
    assert "WORKER_TOKEN" in problem
    assert "ANTHROPIC_API_KEY" in problem
    assert "pass" in fix


def test_check_imports_ok_when_modules_present():
    # anthropic + faster_whisper are required by the test env (pyproject
    # has them under the `worker` extra and we run via `uv run`).
    ok, _, _ = preflight._check_imports(_settings())
    assert ok is True


def test_check_imports_reports_each_missing(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def _fail_anthropic(name, *args, **kwargs):
        if name == "anthropic":
            raise ModuleNotFoundError("No module named 'anthropic'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fail_anthropic)
    ok, problem, fix = preflight._check_imports(_settings())

    assert ok is False
    assert "anthropic" in problem
    assert "uv sync --extra worker" in fix


def test_check_server_passes_when_health_returns_200(monkeypatch):
    @contextmanager
    def _fake_urlopen(_url, timeout: float = 0):  # noqa: ARG001
        class _R:
            status = 200

        yield _R()

    monkeypatch.setattr(preflight.urllib.request, "urlopen", _fake_urlopen)
    ok, problem, _ = preflight._check_server(_settings())
    assert ok is True
    assert problem == ""


def test_check_server_fails_on_unreachable(monkeypatch):
    def _boom(_url, timeout: float = 0):  # noqa: ARG001
        raise ConnectionError("kapow")

    monkeypatch.setattr(preflight.urllib.request, "urlopen", _boom)
    ok, problem, fix = preflight._check_server(
        _settings(server_url="http://nope.invalid")
    )
    assert ok is False
    assert "Cannot reach server" in problem
    assert "network reachability" in fix


def test_check_server_fails_on_non_200(monkeypatch):
    @contextmanager
    def _fake_urlopen(_url, timeout: float = 0):  # noqa: ARG001
        class _R:
            status = 503

        yield _R()

    monkeypatch.setattr(preflight.urllib.request, "urlopen", _fake_urlopen)
    ok, problem, _ = preflight._check_server(_settings())
    assert ok is False
    assert "503" in problem


def test_run_preflight_emits_structured_failure_block(capsys, monkeypatch):
    # Force the very first check to fail deterministically.
    def _failing(_settings):
        return False, "synthetic problem", "synthetic fix"

    monkeypatch.setattr(preflight, "CHECKS", [("imports", _failing)])

    ok = preflight.run_preflight(_settings(), "test-worker")
    assert ok is False

    err = capsys.readouterr().err
    # The block is what the bash wrapper greps for; keep it stable.
    assert "PREFLIGHT_FAILURE" in err
    assert "END_PREFLIGHT_FAILURE" in err
    assert "check: imports" in err
    assert "worker_id: test-worker" in err
    assert "problem: synthetic problem" in err
    assert "fix: synthetic fix" in err


def test_run_preflight_returns_true_when_every_check_passes(monkeypatch, capsys):
    monkeypatch.setattr(
        preflight,
        "CHECKS",
        [("a", lambda _s: (True, "", "")), ("b", lambda _s: (True, "", ""))],
    )
    assert preflight.run_preflight(_settings(), "w") is True
    out = capsys.readouterr().out
    assert "[preflight] a: ok" in out
    assert "[preflight] b: ok" in out


def test_run_preflight_short_circuits_on_first_failure(monkeypatch, capsys):
    calls: list[str] = []

    def _ok(name):
        def _inner(_s):
            calls.append(name)
            return True, "", ""

        return _inner

    def _fail(name):
        def _inner(_s):
            calls.append(name)
            return False, f"{name} bad", "fix"

        return _inner

    monkeypatch.setattr(
        preflight,
        "CHECKS",
        [("first", _ok("first")), ("second", _fail("second")), ("third", _ok("third"))],
    )
    assert preflight.run_preflight(_settings(), "w") is False
    assert calls == ["first", "second"]


def test_preflight_module_does_not_import_alerting():
    # Regression test for the self-heal-first refactor — preflight must
    # never call send_alert directly, it just exits and lets the wrapper
    # decide what to do.
    import inspect

    src = inspect.getsource(preflight)
    assert "send_alert" not in src
    assert "from src.alerting" not in src
