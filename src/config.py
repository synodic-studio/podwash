"""Configuration loading from YAML file and environment variables."""

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class FeedConfig(BaseModel):
    name: str
    url: str
    slug: str
    poll_interval_minutes: int = 60
    max_episodes: int = 0  # 0 = unlimited
    title_includes: list[str] = Field(default_factory=list)


class ProcessingConfig(BaseModel):
    whisper_model: str = "base"
    whisper_compute_type: str = "int8"
    confidence_threshold: float = 0.7
    ad_boundary_padding: float = 0.5
    retention_days: int = 30
    classifier_backend: str = "claude"  # "claude" or "ollama"


class ClaudeConfig(BaseModel):
    model: str = "claude-sonnet-4-5-20250929"
    max_tokens: int = 4096


class OllamaConfig(BaseModel):
    model: str = "llama3.1"
    base_url: str = "http://localhost:11434"
    max_tokens: int = 4096


class WorkerConfig(BaseModel):
    """Settings shared by the queue API (on Vultr) and the worker (on Mac)."""

    # Shared secret for /api/jobs/* endpoints. Set via WORKER_TOKEN env var.
    token: str = ""
    # How long a claim can sit in-flight before it's considered abandoned.
    stale_minutes: int = 45
    # Worker-side only: server base URL to pull jobs from. Override
    # via WORKER_SERVER_URL env var or `worker.server_url` in config.yml.
    server_url: str = "http://localhost:8080"
    # Worker-side only: how long to sleep when the queue is empty.
    idle_poll_seconds: int = 120
    # Worker-side only: max retries per episode before giving up.
    max_retries: int = 3
    # Maximum size of a /result upload in MB. Anything larger gets 413.
    max_upload_mb: int = 500


class AdminConfig(BaseModel):
    """Server-side admin API auth.

    When ``token`` is unset, mutation endpoints under /api/feeds and
    /api/episodes refuse with 503. Set via ADMIN_TOKEN env var or pass
    ``podwash-admin-token``.
    """

    token: str = ""


class Settings(BaseModel):
    base_url: str = "http://localhost:8080"
    data_dir: str = "./data"
    feeds: list[FeedConfig] = Field(default_factory=list)
    processing: ProcessingConfig = Field(default_factory=ProcessingConfig)
    claude: ClaudeConfig = Field(default_factory=ClaudeConfig)
    ollama: OllamaConfig = Field(default_factory=OllamaConfig)
    worker: WorkerConfig = Field(default_factory=WorkerConfig)
    admin: AdminConfig = Field(default_factory=AdminConfig)
    anthropic_api_key: str = ""
    host: str = "0.0.0.0"
    port: int = 8080


def load_settings(config_path: str | None = None) -> Settings:
    """Load settings from YAML config file, overridden by env vars."""
    config_data: dict = {}

    # Find config file
    if config_path is None:
        config_path = os.getenv("CONFIG_PATH", "config.yml")
    path = Path(config_path)
    if path.exists():
        with open(path) as f:
            config_data = yaml.safe_load(f) or {}

    # Env var overrides
    if env_base_url := os.getenv("BASE_URL"):
        config_data["base_url"] = env_base_url
    if env_data_dir := os.getenv("DATA_DIR"):
        config_data["data_dir"] = env_data_dir
    if env_host := os.getenv("HOST"):
        config_data["host"] = env_host
    if env_port := os.getenv("PORT"):
        config_data["port"] = int(env_port)

    settings = Settings(**config_data)
    # Try pass (password-store), fall back to env var
    import shutil
    import subprocess

    api_key = ""
    if shutil.which("pass"):
        _r = subprocess.run(
            ["pass", "show", "anthropic-api-key"], capture_output=True, text=True
        )
        if _r.returncode == 0 and _r.stdout.strip():
            api_key = _r.stdout.strip()
    if not api_key:
        api_key = os.getenv("ANTHROPIC_API_KEY", "")
    settings.anthropic_api_key = api_key

    # Worker-queue shared secret (set on both Vultr and Mac worker)
    if env_token := os.getenv("WORKER_TOKEN"):
        settings.worker.token = env_token
    elif not settings.worker.token and shutil.which("pass"):
        _r = subprocess.run(
            ["pass", "show", "podwash-worker-token"],
            capture_output=True,
            text=True,
        )
        if _r.returncode == 0 and _r.stdout.strip():
            settings.worker.token = _r.stdout.strip()
    if env_server_url := os.getenv("WORKER_SERVER_URL"):
        settings.worker.server_url = env_server_url
    if env_upload_mb := os.getenv("WORKER_MAX_UPLOAD_MB"):
        try:
            settings.worker.max_upload_mb = int(env_upload_mb)
        except ValueError:
            pass

    # Admin token for management mutation endpoints.
    if env_admin := os.getenv("ADMIN_TOKEN") or os.getenv("PODWASH_ADMIN_TOKEN"):
        settings.admin.token = env_admin
    elif not settings.admin.token and shutil.which("pass"):
        _r = subprocess.run(
            ["pass", "show", "podwash-admin-token"],
            capture_output=True,
            text=True,
        )
        if _r.returncode == 0 and _r.stdout.strip():
            settings.admin.token = _r.stdout.strip()

    # Ensure data dir exists
    Path(settings.data_dir).mkdir(parents=True, exist_ok=True)

    return settings
