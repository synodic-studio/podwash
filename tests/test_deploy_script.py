"""Verify deploy.sh shape decisions: port mapping, PORT env, rollback."""

from __future__ import annotations

from pathlib import Path

DEPLOY = Path(__file__).resolve().parents[1] / "scripts" / "deploy.sh"


def test_deploy_script_passes_port_env_to_container():
    body = DEPLOY.read_text()
    # The container must see PORT=container_port so the app binds the
    # right port even when the host port differs.
    assert "-e PORT=${CONTAINER_PORT}" in body


def test_deploy_script_maps_host_to_container_port():
    body = DEPLOY.read_text()
    assert "-p ${HOST_PORT}:${CONTAINER_PORT}" in body


def test_deploy_script_records_old_image_for_rollback():
    body = DEPLOY.read_text()
    assert "OLD_IMAGE" in body
    assert "rolling back" in body


def test_deploy_script_uses_distinct_host_and_container_port_vars():
    body = DEPLOY.read_text()
    assert "HOST_PORT=" in body
    assert "CONTAINER_PORT=" in body
