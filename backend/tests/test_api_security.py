from app.security import password_hash, password_ok, token, verify, redact
from app.workflow import build_graph
from app.worker import execute
from langgraph.checkpoint.memory import InMemorySaver


def test_authentication_required(client):
    for path in ("/runs", "/repositories", "/runs/unknown"):
        assert client.get("/api/v1" + path).status_code == 401


def test_ownership(signed, run_id):
    signed.post("/api/v1/auth/logout")
    signed.post(
        "/api/v1/auth/register", json={"email": "other@example.com", "password": "other-password-123"}
    )
    for path in ("", "/steps", "/artifacts", "/test-results", "/pull-request"):
        assert signed.get(f"/api/v1/runs/{run_id}" + path).status_code == 404
    for path, body in (("/approval", {"decision": "approve"}), ("/cancel", {})):
        assert signed.post(f"/api/v1/runs/{run_id}" + path, json=body).status_code == 404
    assert signed.get("/api/v1/runs").json() == []


def test_approval_before_ready(signed, run_id):
    assert signed.post(f"/api/v1/runs/{run_id}/approval", json={"decision": "approve"}).status_code == 409


def test_cancel_idempotent(signed, run_id):
    assert signed.post(f"/api/v1/runs/{run_id}/cancel").status_code == 200
    assert signed.post(f"/api/v1/runs/{run_id}/cancel").status_code == 200
    assert signed.post(f"/api/v1/runs/{run_id}/approval", json={"decision": "approve"}).status_code == 409


def test_invalid_fields(signed):
    assert signed.post("/api/v1/repositories", json={"full_name": "../../secret"}).status_code == 422
    assert signed.post("/api/v1/runs", json={"repository_id": "x", "issue_text": "short"}).status_code == 422
    assert (
        signed.post(
            "/api/v1/runs",
            json={"repository_id": "x", "issue_text": "a sufficiently long task", "shell": "rm"},
        ).status_code
        == 422
    )


def test_password_and_token():
    hashed = password_hash("correct-password")
    assert password_ok("correct-password", hashed)
    assert not password_ok("incorrect", hashed)
    assert verify(token("test-user")) == "test-user"
    assert verify("junk") is None
    assert verify(token("test-user")[:-5] + "aaaaa") is None


def test_secret_redaction():
    assert "ghp_abcdefghijk" not in redact("ghp_abcdefghijk")
    assert "supersecret" not in redact("API_KEY=supersecret")


def test_artifact_access(signed, run_id):
    execute(build_graph(InMemorySaver()), run_id)
    items = signed.get(f"/api/v1/runs/{run_id}/artifacts").json()
    assert items
    assert signed.get(items[0]["download_url"]).status_code == 200
    signed.post("/api/v1/auth/logout")
    signed.post(
        "/api/v1/auth/register", json={"email": "other@example.com", "password": "other-password-123"}
    )
    assert signed.get(items[0]["download_url"]).status_code == 404
