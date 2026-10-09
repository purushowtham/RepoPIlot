from fastapi.testclient import TestClient
from app.main import app
from app.db import Session, User, Repository


def local_client():
    return TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 12345))


def test_local_workspace_opens_and_reuses_session():
    with local_client() as client:
        first = client.post("/api/v1/auth/workspace").json()
        second = client.post("/api/v1/auth/workspace").json()
        assert first["id"] == second["id"]
        assert client.get("/api/v1/auth/me").status_code == 200
        assert client.get("/api/v1/settings/readiness").json()["can_configure"]
        assert len(client.get("/api/v1/repositories").json()) == 1
    with Session() as db:
        assert db.query(User).count() == 1


def test_local_workspace_recovers_existing_owner_history(signed):
    owner = signed.get("/api/v1/auth/me").json()
    repository = signed.post("/api/v1/repositories", json={"full_name": "demo/login-service"}).json()
    with local_client() as client:
        assert client.post("/api/v1/auth/workspace").json()["id"] == owner["id"]
        assert client.get("/api/v1/repositories").json()[0]["id"] == repository["id"]


def test_remote_guests_are_isolated_and_never_operator(signed):
    owner = signed.get("/api/v1/auth/me").json()
    signed.post("/api/v1/repositories", json={"full_name": "demo/login-service"})
    with TestClient(app) as guest:
        first = guest.post("/api/v1/auth/workspace").json()
        assert first["id"] != owner["id"]
        assert first["email"].startswith("guest-")
        assert guest.post("/api/v1/auth/workspace").json()["id"] == first["id"]
        assert not guest.get("/api/v1/settings/readiness").json()["can_configure"]
        assert guest.post("/api/v1/settings/connections", json={"model": "model"}).status_code == 403
        with Session() as db:
            assert db.get(Repository, guest.get("/api/v1/repositories").json()[0]["id"]).user_id == first["id"]
    with TestClient(app) as other:
        assert other.post("/api/v1/auth/workspace").json()["id"] != first["id"]


def test_guest_cannot_become_operator_when_no_owner_exists(client):
    client.post("/api/v1/auth/workspace")
    assert not client.get("/api/v1/settings/readiness").json()["can_configure"]
    assert client.post("/api/v1/settings/connections", json={"model": "model"}).status_code == 403


def test_cross_origin_and_forwarded_requests_cannot_open_owner(signed):
    owner_id = signed.get("/api/v1/auth/me").json()["id"]
    for headers in ({"origin": "https://example.com"}, {"x-forwarded-for": "127.0.0.1"}, {"host": "example.com"}):
        with local_client() as visitor:
            assert visitor.post("/api/v1/auth/workspace", headers=headers).json()["id"] != owner_id


def test_cloud_loopback_is_not_local_owner(signed, monkeypatch):
    owner_id = signed.get("/api/v1/auth/me").json()["id"]
    monkeypatch.setenv("VERCEL", "1")
    with local_client() as visitor:
        assert visitor.post("/api/v1/auth/workspace").json()["id"] != owner_id
