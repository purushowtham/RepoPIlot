import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from typing import TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt
from langgraph.checkpoint.sqlite import SqliteSaver
from . import config
from .db import Session, Run, Repository, Step, Artifact, TestResult, Approval, PullRequest, now
from .github import GitHub
from .providers import ModelProvider
from .schemas import Plan, Patch, Review
from .patches import apply_patch
from .sandbox import run_tests
from .security import redact


class State(TypedDict, total=False):
    run_id: str
    issue_text: str
    repo_name: str
    base_ref: str
    base_sha: str
    files: dict[str, str]
    plan: dict
    patch: dict
    changed_files: list[str]
    tests: list[dict]
    review: dict
    repair_attempt: int
    failure_context: str
    status: str
    pr: dict
    decision: str


def artifact(run_id, kind, content):
    content = redact(content)
    with Session.begin() as db:
        db.add(
            Artifact(
                run_id=run_id,
                artifact_type=kind,
                content=content,
                sha256=hashlib.sha256(content.encode()).hexdigest(),
            )
        )


def build_graph(checkpointer, model=None, github=None, tester=None):
    model = model or ModelProvider()
    github = github or GitHub()
    tester = tester or run_tests

    def validate(s):
        with Session() as db:
            run = db.get(Run, s["run_id"])
            repo = db.get(Repository, run.repository_id)
            if not repo or repo.user_id != run.created_by:
                raise ValueError("Repository access is unavailable")
        return {"repair_attempt": 0, "tests": [], "repo_name": repo.full_name}

    def plan(s):
        p = model.structured(
            Plan,
            "Plan the issue using metadata only; you have not inspected code yet.",
            {"issue": s["issue_text"], "repository": s["repo_name"]},
        ).model_dump()
        artifact(s["run_id"], "plan", json.dumps(p, indent=2))
        return {"plan": p}

    def analyze(s):
        sha, files = github.snapshot(s["repo_name"], s["base_ref"])
        if any(redact(source) != source for source in files.values()):
            raise ValueError("Recognizable secrets found in source; use a secret-free test repository")
        return {"base_sha": sha, "files": files}

    def code(s):
        p = model.structured(
            Patch,
            "Generate a minimal unified diff with a/ and b/ paths. Only visible .py files may change. Include regression tests. Preserve newline-terminated files. No renames, deletions, binary files, dependency changes or shell tools.",
            {
                "issue": s["issue_text"],
                "plan": s["plan"],
                "files": s["files"],
                "previous_patch": s.get("patch"),
                "failure": s.get("failure_context"),
            },
        ).model_dump()
        if redact(p["diff"]) != p["diff"]:
            raise ValueError("Recognizable secrets found in generated patch")
        artifact(s["run_id"], "patch", p["diff"])
        return {"patch": p}

    def validate_diff(s):
        _, changed = apply_patch(s["patch"]["diff"], s["files"])
        return {"changed_files": changed}

    def test(s):
        files, _ = apply_patch(s["patch"]["diff"], s["files"])
        if config.MODE == "demo":
            result = {
                "command_name": "pytest",
                "command": "python -m pytest -q",
                "exit_code": None,
                "status": "preview",
                "duration_ms": 0,
                "output": "DEMO PREVIEW — no tests were executed. Live mode runs this patch inside Docker and records the real process exit code.",
                "simulated": True,
            }
        else:
            result = tester(files)
        result["output"] = redact(result["output"])
        with Session.begin() as db:
            db.add(TestResult(run_id=s["run_id"], result=result))
        artifact(s["run_id"], "test_log", result["output"])
        return {"tests": s.get("tests", []) + [result]}

    def repair(s):
        if s["repair_attempt"] >= config.MAX_REPAIRS:
            raise ValueError("Repair budget exhausted")
        context = (
            s["tests"][-1]["output"]
            if s["tests"][-1]["status"] not in {"passed", "preview"}
            else json.dumps(s.get("review", {}))
        )
        return {"repair_attempt": s["repair_attempt"] + 1, "failure_context": redact(context)[:12000]}

    def review(s):
        r = model.structured(
            Review,
            "Review acceptance criteria, patch correctness, unrelated changes and actual test evidence. A preview is not executed test evidence. Return structured blocking findings where appropriate.",
            {
                "issue": s["issue_text"],
                "plan": s["plan"],
                "files": s["files"],
                "patch": s["patch"],
                "tests": s["tests"],
            },
        ).model_dump()
        artifact(s["run_id"], "review_report", json.dumps(r, indent=2))
        return {"review": r}

    def fail(s):
        raise ValueError(
            "Tests or review did not pass after two repair attempts. Inspect the saved patch and logs."
        )

    def approval(s):
        with Session.begin() as db:
            run = db.get(Run, s["run_id"])
            decision = db.get(Approval, s["run_id"])
            if not decision:
                run.status = "awaiting_approval"
                run.state = {k: v for k, v in s.items() if k != "files"}
        if not decision:
            interrupt(
                {
                    "run_id": s["run_id"],
                    "message": "Review patch and test evidence before allowing a draft PR.",
                }
            )
        with Session() as db:
            decision = db.get(Approval, s["run_id"])
            if not decision:
                raise ValueError("A durable human approval decision is required")
            return {"decision": decision.decision}

    def publish(s):
        with Session() as db:
            decision = db.get(Approval, s["run_id"])
            run = db.get(Run, s["run_id"])
            if (
                not decision
                or decision.decision != "approve"
                or decision.reviewer_id != run.created_by
                or run.status == "cancelled"
            ):
                raise ValueError("Draft PR creation requires the run owner's explicit approval")
            existing = db.get(PullRequest, s["run_id"])
            if existing:
                return {"pr": {"url": existing.url, "number": existing.number, "branch": existing.branch}}
        if config.MODE == "demo":
            return {"status": "completed"}
        latest = s["tests"][-1]
        if (
            latest["status"] != "passed"
            or latest["exit_code"] != 0
            or latest.get("simulated")
            or any(f["blocking"] for f in s["review"]["findings"])
        ):
            raise ValueError("Real passing sandbox evidence and a non-blocking review are required")
        files, changed = apply_patch(s["patch"]["diff"], s["files"])
        pr = github.create_pr(
            s["repo_name"], s["run_id"], s["base_sha"], s["base_ref"], files, changed, s["plan"]["summary"]
        )
        with Session.begin() as db:
            db.add(PullRequest(run_id=s["run_id"], **pr))
        return {"pr": pr}

    def finalize(s):
        status = "rejected" if s.get("decision") == "reject" else "completed"
        artifact(
            s["run_id"],
            "final_report",
            json.dumps(
                {
                    "summary": s["plan"]["summary"],
                    "changed_files": s["changed_files"],
                    "tests": s["tests"],
                    "review": s["review"],
                    "decision": s["decision"],
                    "pr": s.get("pr"),
                    "mode": config.MODE,
                },
                indent=2,
            ),
        )
        return {"status": status}

    def wrap(name, function):
        def node(s):
            started = time.monotonic()
            with Session() as db:
                run = db.get(Run, s["run_id"])
                if run.status == "cancelled":
                    raise ValueError("Run cancelled")
                if name not in {"await_human_approval", "create_draft_pr", "finalize_run"} and run.started_at:
                    from datetime import datetime, timezone

                    elapsed = (
                        datetime.now(timezone.utc) - datetime.fromisoformat(run.started_at)
                    ).total_seconds()
                    if elapsed > config.RUN_TIMEOUT:
                        raise ValueError("Workflow compute time limit exceeded")
            output = function(s)
            safe_output = json.loads(redact(json.dumps({k: v for k, v in output.items() if k != "files"})))
            if name == "analyze_repository":
                safe_output["relevant_files"] = [
                    {"path": path, "lines": len(source.splitlines()), "snippet": redact(source[:1500])}
                    for path, source in output["files"].items()
                ]
            with Session.begin() as db:
                run = db.get(Run, s["run_id"])
                if run.status == "cancelled":
                    raise ValueError("Run cancelled")
                merged = {**s, **output}
                run.state = json.loads(redact(json.dumps({k: v for k, v in merged.items() if k != "files"})))
                if merged.get("status") in {"completed", "rejected"}:
                    run.status = merged["status"]
                    run.finished_at = now()
                db.add(
                    Step(
                        run_id=s["run_id"],
                        agent_name=name,
                        output=safe_output,
                        duration_ms=int((time.monotonic() - started) * 1000),
                    )
                )
            return output

        return node

    graph = StateGraph(State)
    nodes = {
        "validate_task": validate,
        "plan_task": plan,
        "analyze_repository": analyze,
        "generate_patch": code,
        "validate_patch": validate_diff,
        "run_tests": test,
        "analyze_test_failure": repair,
        "review_patch": review,
        "await_human_approval": approval,
        "create_draft_pr": publish,
        "finalize_run": finalize,
        "handle_error": fail,
    }
    for name, fn in nodes.items():
        graph.add_node(name, wrap(name, fn))
    graph.add_edge(START, "validate_task")
    chain = [
        "validate_task",
        "plan_task",
        "analyze_repository",
        "generate_patch",
        "validate_patch",
        "run_tests",
    ]
    for a, b in zip(chain, chain[1:]):
        graph.add_edge(a, b)
    graph.add_conditional_edges(
        "run_tests",
        lambda s: (
            "review_patch"
            if s["tests"][-1]["status"] in {"passed", "preview"}
            else ("analyze_test_failure" if s["repair_attempt"] < config.MAX_REPAIRS else "handle_error")
        ),
    )
    graph.add_edge("analyze_test_failure", "generate_patch")
    graph.add_conditional_edges(
        "review_patch",
        lambda s: (
            ("analyze_test_failure" if s["repair_attempt"] < config.MAX_REPAIRS else "handle_error")
            if any(f["blocking"] for f in s["review"]["findings"])
            else "await_human_approval"
        ),
    )
    graph.add_conditional_edges(
        "await_human_approval", lambda s: "create_draft_pr" if s["decision"] == "approve" else "finalize_run"
    )
    graph.add_edge("create_draft_pr", "finalize_run")
    graph.add_edge("finalize_run", END)
    graph.add_edge("handle_error", END)
    return graph.compile(checkpointer=checkpointer)


@contextmanager
def durable_graph():
    if config.DATABASE_URL.startswith("postgresql"):
        from langgraph.checkpoint.postgres import PostgresSaver

        url = config.DATABASE_URL.replace("postgresql+psycopg://", "postgresql://")
        with PostgresSaver.from_conn_string(url) as saver:
            saver.setup()
            yield build_graph(saver)
    else:
        with sqlite3.connect(config.DATA / "checkpoints.db", check_same_thread=False) as connection:
            yield build_graph(SqliteSaver(connection))
