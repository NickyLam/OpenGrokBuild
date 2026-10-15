from __future__ import annotations


def test_health_no_auth(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_models_requires_auth(client):
    r = client.get("/v1/models")
    assert r.status_code == 401


def test_models_with_auth(client, auth_headers):
    r = client.get("/v1/models", headers=auth_headers)
    assert r.status_code == 200
    data = r.json()
    assert data["object"] == "list"
    assert any(m["id"] == "grok-4.5" for m in data["data"])


def test_models_refresh_query(client, auth_headers):
    r = client.get("/v1/models?refresh=true", headers=auth_headers)
    assert r.status_code == 200
    assert r.headers["x-grok-model-source"] == "config"
    assert r.headers["x-grok-model-default"] == "grok-4.5"
    assert r.headers["x-grok-model-stale"] == "false"


def test_wrong_key(client):
    r = client.get("/v1/models", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401
