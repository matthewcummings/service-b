from fastapi.testclient import TestClient

from app.api import create_app
from app.config import AppSettings


def test_healthz(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_version_reports_build_and_env(client):
    assert client.get("/version").json() == {
        "service": "service-b",
        "env": "test",
        "branch": "main",
        "sha": "abc123",
    }


def test_readyz_ok_when_db_reachable(client):
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_503_but_healthz_200_when_db_unreachable(db_settings, monkeypatch):
    monkeypatch.setenv("DB_PORT", "1")  # nothing listens there
    with TestClient(create_app(AppSettings())) as client:
        assert client.get("/readyz").status_code == 503
        assert client.get("/healthz").status_code == 200


def test_items_crud_round_trip(client):
    created = client.post("/items", json={"name": "widget", "description": "a test widget"})
    assert created.status_code == 201
    item = created.json()
    assert item["name"] == "widget"
    assert item["description"] == "a test widget"
    assert item["created_at"]
    item_url = f"/items/{item['id']}"

    assert client.get(item_url).json() == item
    assert client.get("/items").json() == [item]

    response = client.put(item_url, json={"name": "gadget"})
    assert response.status_code == 200
    replaced = response.json()
    assert replaced["id"] == item["id"]
    assert replaced["name"] == "gadget"
    assert replaced["description"] is None  # PUT replaces the whole item

    assert client.delete(item_url).status_code == 204
    assert client.get(item_url).status_code == 404
    assert client.get("/items").json() == []


def test_items_missing_and_invalid(client):
    assert client.get("/items/999").status_code == 404
    assert client.put("/items/999", json={"name": "x"}).status_code == 404
    assert client.delete("/items/999").status_code == 404
    assert client.post("/items", json={"name": ""}).status_code == 422
    assert client.post("/items", json={}).status_code == 422


def test_routes_mounted_under_path_prefix(migrated_db):
    # The ALB forwards /a/... unchanged, so every route (docs too) must live under the prefix.
    app = create_app(AppSettings(path_prefix="/a"))
    with TestClient(app) as client:
        assert client.get("/a/healthz").status_code == 200
        assert client.get("/a/readyz").status_code == 200
        assert client.get("/a/version").status_code == 200
        assert client.post("/a/items", json={"name": "prefixed"}).status_code == 201
        assert client.get("/a/openapi.json").status_code == 200

        assert client.get("/healthz").status_code == 404
        assert client.get("/items").status_code == 404
