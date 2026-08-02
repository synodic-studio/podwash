"""Pre-flight checks the worker runs before entering its main loop.

Designed to make today's silent-failure modes impossible to miss:
  - Missing Python deps (Apr 2026: `.venv` lost the `worker` extra and
    launchd silently throttle-restarted for 3 days)
  - Missing/empty WORKER_TOKEN or ANTHROPIC_API_KEY
  - Server unreachable (Tailscale down, container stopped, wrong URL)
  - Anthropic API broken (revoked key, quota exhausted, outage)

Each check returns (ok, problem, fix). On the first failure we print a
structured `PREFLIGHT_FAILURE` block to stderr and the caller exits 1.
The launchd wrapper picks that block up, asks Claude Code headless to
self-heal, and only escalates to Telegram if the heal attempt fails or
times out. Preflight itself never sends Telegram — that keeps escalation
in a single place (the wrapper) so we can't double-page.
"""

from __future__ import annotations

import sys
import urllib.request
from typing import Callable

from src.config import Settings

CheckResult = tuple[bool, str, str]
Check = Callable[[Settings], CheckResult]


def _check_imports(_settings: Settings) -> CheckResult:
    """Verify the heavy worker-only deps actually import."""
    missing: list[str] = []
    for module in ("anthropic", "faster_whisper"):
        try:
            __import__(module)
        except Exception as exc:
            missing.append(f"{module} ({type(exc).__name__}: {exc})")
    if missing:
        return (
            False,
            f"Worker-only Python deps failed to import: {', '.join(missing)}",
            "Run `uv sync --extra worker` in the repo. The launchd plist "
            "uses `uv run --extra worker` so a fresh boot should self-heal "
            "— if this alert keeps firing, the .venv is locked or uv can't "
            "reach the index.",
        )
    return True, "", ""


def _check_secrets(settings: Settings) -> CheckResult:
    """Verify the secrets the loop needs are present and non-empty."""
    missing: list[str] = []
    if not settings.worker.token:
        missing.append("WORKER_TOKEN (or `pass show podwash-worker-token`)")
    if settings.processing.classifier_backend == "claude" and not settings.anthropic_api_key:
        missing.append("ANTHROPIC_API_KEY (or `pass show anthropic-api-key`)")
    if missing:
        return (
            False,
            f"Required secrets are unset: {', '.join(missing)}",
            "Populate them in pass (preferred) or in the worker's "
            "environment. The plist intentionally has no plaintext "
            "secrets — fix it at the source, not in the plist.",
        )
    return True, "", ""


def _check_server(settings: Settings) -> CheckResult:
    """Verify the queue API on Vultr is actually reachable."""
    url = settings.worker.server_url.rstrip("/") + "/health"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            if resp.status != 200:
                return (
                    False,
                    f"Server /health returned HTTP {resp.status} at {url}",
                    "Check the podwash container on the server "
                    "(`docker ps`, `docker logs podwash`).",
                )
    except Exception as exc:
        return (
            False,
            f"Cannot reach server at {url}: {type(exc).__name__}: {exc}",
            "Confirm network reachability to the server and that the "
            "podwash container is running. If the address changed, "
            "update worker.server_url in config.yml or the "
            "WORKER_SERVER_URL env var.",
        )
    return True, "", ""


def _check_classifier(settings: Settings) -> CheckResult:
    """Verify the configured classifier backend is reachable and answers.

    Whichever backend is selected, a worker that boots without it just
    claims jobs and fails them one at a time, burning retries. Fail here
    instead so the crash wrapper can escalate.
    """
    if settings.processing.classifier_backend != "claude":
        return _check_openai_compatible(settings)
    try:
        from anthropic import Anthropic
    except Exception as exc:
        return (
            False,
            f"anthropic SDK import failed during API check: {exc}",
            "Run `uv sync --extra worker`.",
        )
    try:
        client = Anthropic(api_key=settings.anthropic_api_key)
        client.messages.count_tokens(
            model=settings.claude.model,
            messages=[{"role": "user", "content": "ping"}],
        )
    except Exception as exc:
        return (
            False,
            f"Anthropic API call failed: {type(exc).__name__}: {exc}",
            "Check the key (`pass show anthropic-api-key`), billing/quota "
            "in the Anthropic console, and that the model "
            f"`{settings.claude.model}` is still available.",
        )
    return True, "", ""


def _check_openai_compatible(settings: Settings) -> CheckResult:
    """Verify the OpenAI-compatible endpoint is up and serving the model."""
    cfg = settings.litellm
    try:
        import httpx

        headers = (
            {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}
        )
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                f"{cfg.base_url.rstrip('/')}/chat/completions",
                headers=headers,
                json={
                    "model": cfg.model,
                    "max_tokens": 16,
                    "messages": [{"role": "user", "content": "Reply with OK."}],
                    "thinking": {"type": "disabled"},
                    "extra_body": {"thinking": {"type": "disabled"}},
                },
            )
        resp.raise_for_status()
        if not (resp.json()["choices"][0]["message"].get("content") or ""):
            return (
                False,
                f"Model `{cfg.model}` returned empty content on a ping.",
                "The model is likely spending its whole budget on reasoning. "
                "Confirm reasoning is disabled for this model in the proxy "
                "config, or raise processing max_tokens.",
            )
    except Exception as exc:
        return (
            False,
            f"Classifier endpoint failed: {type(exc).__name__}: {exc}",
            f"Confirm the proxy at {cfg.base_url} is running and serving "
            f"`{cfg.model}`. For a local LiteLLM proxy check it is up on "
            "that port; `curl {base}/models` lists what it serves.".format(
                base=cfg.base_url.rstrip("/")
            ),
        )
    return True, "", ""


CHECKS: list[tuple[str, Check]] = [
    ("imports", _check_imports),
    ("secrets", _check_secrets),
    ("server", _check_server),
    ("classifier", _check_classifier),
]


def run_preflight(settings: Settings, worker_id: str) -> bool:
    """Run every check. On the first failure, emit a structured failure
    block to stderr and return False. The launchd wrapper parses that
    block and decides what to do next (self-heal via Claude, then
    optionally Telegram).

    Returns True iff every check passed.
    """
    for name, check in CHECKS:
        ok, problem, fix = check(settings)
        if not ok:
            print(
                "PREFLIGHT_FAILURE\n"
                f"check: {name}\n"
                f"worker_id: {worker_id}\n"
                f"server_url: {settings.worker.server_url}\n"
                f"problem: {problem}\n"
                f"fix: {fix}\n"
                "END_PREFLIGHT_FAILURE",
                file=sys.stderr,
                flush=True,
            )
            return False
        print(f"[preflight] {name}: ok")
    return True
