import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="repopilot-tests-")
os.environ["REPOPILOT_MODE"] = "demo"
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.db import Base, engine


@pytest.fixture(autouse=True)
def database():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def signed(client):
    assert (
        client.post(
            "/api/v1/auth/register", json={"email": "owner@example.com", "password": "correct-password-123"}
        ).status_code
        == 201
    )
    return client


@pytest.fixture
def run_id(signed):
    repo = signed.post("/api/v1/repositories", json={"full_name": "demo/login-service"}).json()
    response = signed.post(
        "/api/v1/runs",
        json={
            "repository_id": repo["id"],
            "issue_text": "Handle missing email safely and add regression tests.",
        },
    )
    assert response.status_code == 202
    return response.json()["id"]
