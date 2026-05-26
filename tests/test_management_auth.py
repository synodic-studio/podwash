"""Management API requires the admin bearer token; public routes don't."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.config import Settings
from src.database import queries
from src.database.models import Feed


@pytest.fixture
def app(tmp_path):
    s = Settings()
    s.data_dir = str(tmp_path)
    s.admin.token = "admin-secret"
    return create_app(s)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


def test_get_feeds_without_token_returns_401(client):
    r = client.get("/api/feeds")
    assert r.status_code == 401


def test_get_feeds_with_wrong_token_returns_403(client):
    r = client.get("/api/feeds", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 403


def test_get_feeds_with_correct_token_returns_200(client):
    r = client.get("/api/feeds", headers={"Authorization": "Bearer admin-secret"})
    assert r.status_code == 200


def test_delete_feed_requires_admin(client, app):
    feed_id = queries.upsert_feed(
        app.state.db,
        Feed(name="show", source_url="http://x", slug="show"),
    )
    r = client.delete(f"/api/feeds/{feed_id}")
    assert r.status_code == 401
    r2 = client.delete(
        f"/api/feeds/{feed_id}", headers={"Authorization": "Bearer admin-secret"}
    )
    assert r2.status_code == 200


def test_public_rss_route_stays_public(client, app):
    queries.upsert_feed(
        app.state.db,
        Feed(name="show", source_url="http://x", slug="public-show"),
    )
    r = client.get("/feeds/public-show.xml")
    assert r.status_code == 200


def test_health_stays_public(client):
    assert client.get("/health").status_code == 200


def test_admin_disabled_returns_503(tmp_path):
    s = Settings()
    s.data_dir = str(tmp_path)
    s.admin.token = ""
    app = create_app(s)
    with TestClient(app) as c:
        r = c.get("/api/feeds")
    assert r.status_code == 503
