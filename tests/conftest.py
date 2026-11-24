"""Keep application tests independent of live background polling.

Scheduler behavior is exercised directly in its unit tests. Starting its real
thread from TestClient can leak an in-flight poll past lifespan shutdown and
mutate another test's process-global failure counters.
"""

from unittest.mock import Mock

import pytest

from src.api import app as app_module


@pytest.fixture(autouse=True)
def _disable_app_background_scheduler(monkeypatch):
    scheduler = Mock(spec=["shutdown"])
    monkeypatch.setattr(app_module, "start_scheduler", lambda conn, settings: scheduler)
