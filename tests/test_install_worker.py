"""Verify install-worker plist templating survives special characters."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def repo_root():
    return Path(__file__).resolve().parents[1]


def test_install_worker_script_uses_python_templating(repo_root):
    body = (repo_root / "deploy" / "install-worker.sh").read_text()
    # We replaced sed with python3 + xml.sax.saxutils.escape — make sure
    # we don't regress to fragile sed substitution.
    assert "python3 - " in body
    assert "xml.sax.saxutils" in body


def test_plist_template_renders_special_characters(tmp_path, repo_root):
    """Render the worker plist via the templating snippet directly."""
    # The watchdog template is the one carrying __SERVER_URL__.
    src = repo_root / "deploy" / "podwash-worker-watchdog.plist"
    if not src.exists():
        pytest.skip("watchdog plist template missing")
    dst = tmp_path / "out.plist"
    env = {
        "LABEL": "local.podwash.worker",
        "UV_PATH": "/usr/local/bin/uv",
        "REPO_PATH": "/repo",
        "LOGDIR_PATH": "/log",
        "HOME_PATH": "/Users/x",
        "SERVER_URL_VAL": "https://example.com/a?b=1&c=2",
        "ALERT_CHAT_ID_VAL": "-100123",
        "ALERT_THREAD_ID_VAL": "",
    }
    script = (
        "import os, sys\n"
        "from xml.sax.saxutils import escape\n"
        "src, dst = sys.argv[1], sys.argv[2]\n"
        "mapping = {\n"
        '    "__LABEL__": os.environ["LABEL"],\n'
        '    "__UV__": os.environ["UV_PATH"],\n'
        '    "__REPO__": os.environ["REPO_PATH"],\n'
        '    "__LOGDIR__": os.environ["LOGDIR_PATH"],\n'
        '    "__HOME__": os.environ["HOME_PATH"],\n'
        '    "__SERVER_URL__": os.environ["SERVER_URL_VAL"],\n'
        '    "__ALERT_CHAT_ID__": os.environ["ALERT_CHAT_ID_VAL"],\n'
        '    "__ALERT_THREAD_ID__": os.environ["ALERT_THREAD_ID_VAL"],\n'
        "}\n"
        "with open(src) as f:\n"
        "    body = f.read()\n"
        "for placeholder, value in mapping.items():\n"
        "    body = body.replace(placeholder, escape(value))\n"
        "with open(dst, 'w') as f:\n"
        "    f.write(body)\n"
    )
    subprocess.run(
        ["python3", "-c", script, str(src), str(dst)],
        env={**env, "PATH": "/usr/bin:/usr/local/bin"},
        check=True,
    )
    rendered = dst.read_text()
    # & must be XML-escaped, not raw.
    assert "&amp;" in rendered
    assert "https://example.com/a?b=1&c=2" not in rendered
    if shutil.which("plutil"):
        subprocess.run(["plutil", "-lint", str(dst)], check=True)
