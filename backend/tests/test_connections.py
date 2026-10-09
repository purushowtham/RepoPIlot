import pytest
from app import config
from app.connections import settings
from app.schemas import Plan


@pytest.fixture(autouse=True)
def isolate_vault(monkeypatch):
    for name in ("connections.enc", "connections.key"):
        (config.DATA / name).unlink(missing_ok=True)
    monkeypatch.setattr(
        "app.connections.sandbox_status", lambda: {"ready": True, "message": "Isolated sandbox test double"}
    )
    yield
    for name in ("connections.enc", "connections.key"):
        (config.DATA / name).unlink(missing_ok=True)


def test_readiness_requires_login(client):
    assert client.get("/api/v1/settings/readiness").status_code == 401


def test_credentials_encrypted_and_not_returned(signed):
    secret = "test-ai-private-value-12345"
    result = signed.post(
        "/api/v1/settings/connections",
        json={
            "model": "test-model",
            "llm_api_key": secret,
            "github_token": "test-github-private-value-12345",
        },
    )
    assert result.status_code == 200
    assert secret not in result.text
    assert secret.encode() not in (config.DATA / "connections.enc").read_bytes()
    assert (config.DATA / "connections.enc").stat().st_mode & 0o777 == 0o600
    assert (config.DATA / "connections.key").stat().st_mode & 0o777 == 0o600
    assert settings()["llm_api_key"] == secret
    view = signed.get("/api/v1/settings/readiness")
    assert secret not in view.text
    assert view.json()["ai"]["configured"]
    assert not view.json()["ai"]["verified"]


def test_nonowner_cannot_configure(signed):
    signed.post(
        "/api/v1/auth/register", json={"email": "other@example.com", "password": "other-password-123"}
    )
    assert (
        signed.post(
            "/api/v1/settings/connections", json={"model": "model", "llm_api_key": "secret"}
        ).status_code
        == 403
    )
    assert signed.post("/api/v1/settings/verify").status_code == 403
    assert not signed.get("/api/v1/settings/readiness").json()["can_configure"]


def test_live_mode_cannot_queue_without_verified_connections(signed, monkeypatch):
    monkeypatch.setattr(config, "MODE", "live")
    response = signed.post(
        "/api/v1/runs", json={"repository_id": "unknown", "issue_text": "Fix a real repository issue safely"}
    )
    assert response.status_code == 409
    assert "Live setup" in response.json()["detail"]


def test_verify_real_integrations_and_invalidate_after_rotation(signed, monkeypatch):
    monkeypatch.setattr(config, "MODE", "live")
    from app.db import Session, Repository, User

    with Session.begin() as db:
        user = db.query(User).one()
        repo = Repository(user_id=user.id, full_name="example/live-repo", default_branch="main")
        db.add(repo)
        db.flush()
        repo_id = repo.id
    called = []

    def structured(*args, **kwargs):
        called.append(kwargs["max_tokens"])
        return Plan(
            summary="Connection check",
            acceptance_criteria=["Input is validated"],
            steps=["Inspect input"],
            risks=[],
        )

    monkeypatch.setattr("app.main.ModelProvider.structured", structured)
    monkeypatch.setattr("app.main.GitHub.request", lambda *a, **kw: {"login": "test-user"})
    signed.post(
        "/api/v1/settings/connections",
        json={"model": "model", "llm_api_key": "private-key", "github_token": "private-token"},
    )
    result = signed.post("/api/v1/settings/verify")
    assert result.status_code == 200
    assert result.json()["ready"]
    assert called == [800]
    response = signed.post(
        "/api/v1/runs", json={"repository_id": repo_id, "issue_text": "Fix a real repository issue safely"}
    )
    assert response.status_code == 202
    rid = response.json()["id"]
    assert signed.get(f"/api/v1/runs/{rid}").json()["mode"] == "live"
    assert signed.post("/api/v1/settings/connections", json={"model": "replacement"}).status_code == 409
    signed.post(f"/api/v1/runs/{rid}/cancel")
    assert signed.post("/api/v1/settings/connections", json={"model": "replacement"}).status_code == 200
    assert not signed.get("/api/v1/settings/readiness").json()["ready"]


def test_verify_error_does_not_leak_secret(signed, monkeypatch):
    monkeypatch.setattr(config, "MODE", "live")
    secret = "test-ai-private-value-12345"
    signed.post(
        "/api/v1/settings/connections",
        json={"model": "model", "llm_api_key": secret, "github_token": "private-token"},
    )

    def fail(*args, **kwargs):
        raise RuntimeError("provider returned " + secret)

    monkeypatch.setattr("app.main.ModelProvider.structured", fail)
    monkeypatch.setattr("app.main.GitHub.request", fail)
    result = signed.post("/api/v1/settings/verify")
    assert result.status_code == 200
    assert secret not in result.text
    assert not result.json()["ready"]


def test_claim_demo_workspace(client):
    account = client.post(
        "/api/v1/auth/register", json={"email": "demo-test@repopilot.local", "password": "temporary-password"}
    ).json()
    assert client.post("/api/v1/settings/connections", json={"model": "model"}).status_code == 409
    result = client.post(
        "/api/v1/auth/claim", json={"email": "permanent@example.com", "password": "permanent-password"}
    )
    assert result.status_code == 200
    assert result.json()["id"] == account["id"]
    assert client.post("/api/v1/settings/connections", json={"model": "model"}).status_code == 200


def test_saved_secret_redacted(signed):
    from app.security import redact

    secret = "private-value-without-known-prefix"
    signed.post("/api/v1/settings/connections", json={"model": "model", "llm_api_key": secret})
    assert secret not in redact("source includes " + secret)


def test_invalid_secret_field_never_echoed(signed):
    value = "a-private-key-value" * 100
    response = signed.post("/api/v1/settings/connections", json={"model": "model", "llm_api_key": value})
    assert response.status_code == 422
    assert value not in response.text


def test_invalid_password_never_echoed(client):
    value = "a-secret-password-value" * 100
    response = client.post("/api/v1/auth/register", json={"email": "test@example.com", "password": value})
    assert response.status_code == 422
    assert value not in response.text
