import sqlite3
from langgraph.checkpoint.sqlite import SqliteSaver
from app import config
from app.db import Session, Run, PullRequest, now
from app.workflow import build_graph
from app.worker import execute
from app.providers import ModelProvider
from app.schemas import Review


class FixtureModel(ModelProvider):
    # Fixture outputs remain deterministic even when sandbox routing is live.
    def structured(self, schema, instruction, context):
        from app import providers

        original = providers.MODE
        providers.MODE = "demo"
        try:
            return super().structured(schema, instruction, context)
        finally:
            providers.MODE = original


class GitHubSpy:
    calls = 0

    def snapshot(self, *_):
        from app.providers import demo_files

        return "a" * 40, demo_files()

    def create_pr(self, *_):
        self.calls += 1
        return {"url": "https://github.com/example/repo/pull/1", "number": 1, "branch": "repopilot/test"}


def begin(run_id):
    with Session.begin() as db:
        run = db.get(Run, run_id)
        run.status = "running"
        run.started_at = now()


def graph():
    return build_graph(SqliteSaver(sqlite3.connect(":memory:", check_same_thread=False)))


def test_demo_pause_approve_idempotent(signed, run_id):
    g = graph()
    begin(run_id)
    execute(g, run_id)
    detail = signed.get(f"/api/v1/runs/{run_id}").json()
    assert detail["status"] == "awaiting_approval"
    assert detail["state"]["tests"][0]["exit_code"] is None
    assert detail["state"]["tests"][0]["simulated"] is True
    assert detail["pull_request"] is None
    assert signed.post(f"/api/v1/runs/{run_id}/approval", json={"decision": "approve"}).status_code == 200
    begin(run_id)
    execute(g, run_id)
    assert signed.get(f"/api/v1/runs/{run_id}").json()["status"] == "completed"
    assert signed.post(f"/api/v1/runs/{run_id}/approval", json={"decision": "approve"}).status_code == 200
    assert signed.post(f"/api/v1/runs/{run_id}/approval", json={"decision": "reject"}).status_code == 409
    with Session() as db:
        assert db.get(PullRequest, run_id) is None


def test_reject_finishes_without_pr(signed, run_id):
    g = graph()
    begin(run_id)
    execute(g, run_id)
    signed.post(f"/api/v1/runs/{run_id}/approval", json={"decision": "reject"})
    begin(run_id)
    execute(g, run_id)
    assert signed.get(f"/api/v1/runs/{run_id}").json()["status"] == "rejected"


def test_checkpoint_reopen(signed, run_id, tmp_path):
    path = tmp_path / "graph.sqlite"
    with sqlite3.connect(path, check_same_thread=False) as c:
        g = build_graph(SqliteSaver(c))
        begin(run_id)
        execute(g, run_id)
    signed.post(f"/api/v1/runs/{run_id}/approval", json={"decision": "approve"})
    with sqlite3.connect(path, check_same_thread=False) as c:
        g = build_graph(SqliteSaver(c))
        begin(run_id)
        execute(g, run_id)
    assert signed.get(f"/api/v1/runs/{run_id}").json()["status"] == "completed"


def test_no_publish_without_approval_live(signed, run_id, monkeypatch):
    monkeypatch.setattr(config, "MODE", "live")
    spy = GitHubSpy()
    g = build_graph(
        SqliteSaver(sqlite3.connect(":memory:", check_same_thread=False)),
        model=FixtureModel(),
        github=spy,
        tester=lambda _: {
            "command": "pytest",
            "status": "passed",
            "exit_code": 0,
            "output": "3 passed (test double)",
            "duration_ms": 1,
            "simulated": False,
        },
    )
    begin(run_id)
    execute(g, run_id)
    assert spy.calls == 0
    assert signed.get(f"/api/v1/runs/{run_id}").json()["status"] == "awaiting_approval"
    signed.post(f"/api/v1/runs/{run_id}/approval", json={"decision": "approve"})
    begin(run_id)
    execute(g, run_id)
    assert spy.calls == 1
    execute(g, run_id)
    assert spy.calls == 1


def test_failure_loop_bounded(signed, run_id, monkeypatch):
    monkeypatch.setattr(config, "MODE", "live")
    calls = []

    def failing(_):
        calls.append(True)
        return {
            "command": "pytest",
            "status": "failed",
            "exit_code": 1,
            "output": "assertion failed",
            "duration_ms": 1,
            "simulated": False,
        }

    spy = GitHubSpy()
    g = build_graph(
        SqliteSaver(sqlite3.connect(":memory:", check_same_thread=False)),
        model=FixtureModel(),
        github=spy,
        tester=failing,
    )
    begin(run_id)
    execute(g, run_id)
    d = signed.get(f"/api/v1/runs/{run_id}").json()
    assert d["status"] == "failed"
    assert d["state"]["repair_attempt"] == 2
    assert len(calls) == 3
    assert spy.calls == 0


def test_review_loop_bounded(signed, run_id):
    class BlockingModel(FixtureModel):
        def structured(self, schema, instruction, context):
            if schema is Review:
                return Review(
                    summary="Needs repair",
                    findings=[
                        {
                            "severity": "critical",
                            "description": "Missing regression coverage",
                            "blocking": True,
                        }
                    ],
                )
            return super().structured(schema, instruction, context)

    g = build_graph(SqliteSaver(sqlite3.connect(":memory:", check_same_thread=False)), model=BlockingModel())
    begin(run_id)
    execute(g, run_id)
    d = signed.get(f"/api/v1/runs/{run_id}").json()
    assert d["status"] == "failed"
    assert d["state"]["repair_attempt"] == 2


def test_cancel_blocks_workflow(signed, run_id):
    signed.post(f"/api/v1/runs/{run_id}/cancel")
    execute(graph(), run_id)
    assert signed.get(f"/api/v1/runs/{run_id}").json()["status"] == "cancelled"


def test_timeout_fails(signed, run_id):
    from datetime import datetime, timezone, timedelta

    begin(run_id)
    with Session.begin() as db:
        db.get(Run, run_id).started_at = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    execute(graph(), run_id)
    d = signed.get(f"/api/v1/runs/{run_id}").json()
    assert d["status"] == "failed"
    assert "time limit" in d["error_message"]


def test_simulated_evidence_cannot_publish(signed, run_id, monkeypatch):
    monkeypatch.setattr(config, "MODE", "live")
    spy = GitHubSpy()
    g = build_graph(
        SqliteSaver(sqlite3.connect(":memory:", check_same_thread=False)),
        model=FixtureModel(),
        github=spy,
        tester=lambda _: {
            "command": "pytest",
            "status": "passed",
            "exit_code": 0,
            "output": "fixture result",
            "duration_ms": 1,
            "simulated": True,
        },
    )
    begin(run_id)
    execute(g, run_id)
    signed.post(f"/api/v1/runs/{run_id}/approval", json={"decision": "approve"})
    begin(run_id)
    execute(g, run_id)
    assert spy.calls == 0
    assert signed.get(f"/api/v1/runs/{run_id}").json()["status"] == "failed"


def test_secret_source_rejected_before_checkpoint(signed, run_id):
    class SecretRepo(GitHubSpy):
        def snapshot(self, *_):
            return "a" * 40, {"auth.py": 'API_KEY="a-sensitive-value"\n'}

    g = build_graph(
        SqliteSaver(sqlite3.connect(":memory:", check_same_thread=False)),
        model=FixtureModel(),
        github=SecretRepo(),
    )
    begin(run_id)
    execute(g, run_id)
    d = signed.get(f"/api/v1/runs/{run_id}").json()
    assert d["status"] == "failed"
    assert "secrets found" in d["error_message"]
    snapshot = g.get_state({"configurable": {"thread_id": run_id}})
    assert "files" not in snapshot.values


def test_invalid_patch_never_reaches_tester(signed, run_id, monkeypatch):
    from app.schemas import Patch

    class InvalidModel(FixtureModel):
        def structured(self, schema, instruction, context):
            if schema is Patch:
                return Patch(
                    diff="--- /dev/null\n+++ b/../escape.py\n@@ -0,0 +1 @@\n+bad\n", explanation="unsafe"
                )
            return super().structured(schema, instruction, context)

    monkeypatch.setattr(config, "MODE", "live")
    calls = []
    g = build_graph(
        SqliteSaver(sqlite3.connect(":memory:", check_same_thread=False)),
        model=InvalidModel(),
        github=GitHubSpy(),
        tester=lambda _: calls.append(True),
    )
    begin(run_id)
    execute(g, run_id)
    assert calls == []
    assert signed.get(f"/api/v1/runs/{run_id}").json()["status"] == "failed"
