"""Flask smoke tests over a seeded temp database (routes render, CSRF guard)."""
from __future__ import annotations

import pytest

import server


@pytest.fixture
def client(temp_paths, monkeypatch):
    # Don't hit the network for the background update check during tests.
    monkeypatch.setattr(server.updater, "check", lambda force=False: {})
    app = server.create_app()
    app.config.update(TESTING=True)
    return app.test_client(), temp_paths["ids"]


def test_all_pages_render(client):
    c, ids = client
    pages = ["/", "/dashboard", "/compliance", "/compliance/report", "/runs",
             "/analysis", "/search", "/healthz", f"/requests/{ids[0]}"]
    for url in pages:
        assert c.get(url).status_code == 200, url


def test_missing_request_404s(client):
    c, _ = client
    assert c.get("/requests/DOESNOTEXIST").status_code == 404


def test_search_finds_seeded_content(client):
    c, _ = client
    body = c.get("/search?q=rezoning").get_data(as_text=True)
    assert "match" in body
    assert "<mark>" in body or "rezoning" in body.lower()


def test_csrf_same_origin_guard(client):
    c, ids = client
    rid = ids[0]
    # No Origin/Referer (e.g. the app's own form) -> allowed (redirect)
    assert c.post(f"/requests/{rid}/override", data={}).status_code in (302, 303)
    # Cross-origin POST -> blocked
    resp = c.post(f"/requests/{rid}/override", data={},
                  headers={"Origin": "http://evil.example"})
    assert resp.status_code == 403


def test_manual_backup_action(client):
    c, _ = client
    assert c.post("/actions/backup").status_code in (302, 303)
