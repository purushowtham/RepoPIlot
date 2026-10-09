from contextlib import asynccontextmanager
import os
from urllib.parse import urlparse
from uuid import uuid4
from fastapi import FastAPI, Depends, HTTPException, Response, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import select, update, func
from sqlalchemy.exc import IntegrityError
from . import config
from .db import (
    Base,
    engine,
    Session,
    User,
    Repository,
    Run,
    Step,
    Artifact,
    TestResult,
    Approval,
    PullRequest,
    now,
)
from .schemas import Credentials, RepoInput, RunInput, Decision, ConnectionInput
from .security import password_hash, password_ok, token, verify, redact
from .github import GitHub
from .connections import readiness, settings, save_connections, record_verification, fingerprint
from .schemas import Plan
from .providers import ModelProvider


@asynccontextmanager
async def lifespan(app):
    # Development SQLite convenience; PostgreSQL uses Alembic migrations.
    if config.DATABASE_URL.startswith("sqlite"):
        Base.metadata.create_all(engine)
    yield


app = FastAPI(title="RepoPilot", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        o.strip()
        for o in __import__("os")
        .getenv("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173,http://localhost:4173")
        .split(",")
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)
PREFIX = "/api/v1"


def user(request: Request):
    identity = verify(request.cookies.get("repopilot_session", ""))
    with Session() as db:
        account = db.get(User, identity) if identity else None
        if not account:
            raise HTTPException(401, "Sign in to continue")
        return account.id


def owned_run(db, run_id, account):
    run = db.get(Run, run_id)
    if not run or run.created_by != account:
        raise HTTPException(404, "Run not found")
    return run


def run_view(run, db):
    repo = db.get(Repository, run.repository_id)
    approval = db.get(Approval, run.id)
    pr = db.get(PullRequest, run.id)
    return {
        "id": run.id,
        "repository_id": run.repository_id,
        "repository": repo.full_name,
        "issue_text": run.issue_text,
        "base_ref": run.base_ref,
        "status": run.status,
        "state": run.state,
        "error_message": run.error_message,
        "created_at": run.created_at,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "approval": {
            "decision": approval.decision,
            "comment": approval.comment,
            "created_at": approval.created_at,
        }
        if approval
        else None,
        "pull_request": {"url": pr.url, "number": pr.number} if pr else None,
        "mode": run.state.get(
            "mode", "demo" if any(t.get("simulated") for t in run.state.get("tests", [])) else config.MODE
        ),
    }


def sign_in(response, account):
    response.set_cookie(
        "repopilot_session",
        token(account.id),
        httponly=True,
        secure=__import__("os").getenv("COOKIE_SECURE", "false") == "true",
        samesite="strict",
        max_age=86400,
    )
    return {"id": account.id, "email": account.email}


@app.post(PREFIX + "/auth/register", status_code=201)
def register(body: Credentials, response: Response):
    with Session.begin() as db:
        account = User(email=body.email.lower(), password_hash=password_hash(body.password))
        db.add(account)
        try:
            db.flush()
        except IntegrityError:
            raise HTTPException(409, "Account already exists")
        return sign_in(response, account)


@app.post(PREFIX + "/auth/login")
def login(body: Credentials, response: Response):
    with Session() as db:
        account = db.scalar(select(User).where(User.email == body.email.lower()))
        if not account or not password_ok(body.password, account.password_hash):
            raise HTTPException(401, "Invalid email or password")
        return sign_in(response, account)


@app.post(PREFIX + "/auth/logout")
def logout(response: Response):
    response.delete_cookie("repopilot_session")
    return {"ok": True}


@app.get(PREFIX + "/auth/me")
def me(account=Depends(user)):
    with Session() as db:
        u = db.get(User, account)
        return {"id": account, "email": u.email}


def local_workspace_request(request: Request):
    # Owner access is limited to direct, loopback requests with a local Host and
    # Origin. A forwarded/cloud request must never become the local owner.
    local_hosts = {"localhost", "127.0.0.1", "::1"}
    origin = request.headers.get("origin")
    return bool(
        os.getenv("VERCEL") != "1"
        and request.client
        and request.client.host in {"127.0.0.1", "::1"}
        and request.url.hostname in local_hosts
        and not any(name in request.headers for name in ("forwarded", "x-forwarded-for", "x-vercel-id"))
        and (not origin or urlparse(origin).hostname in local_hosts)
        and request.headers.get("sec-fetch-site") != "cross-site"
    )


@app.post(PREFIX + "/auth/workspace")
def open_workspace(request: Request, response: Response):
    identity = verify(request.cookies.get("repopilot_session", ""))
    with Session.begin() as db:
        account = db.get(User, identity) if identity else None
        if not account:
            if local_workspace_request(request):
                account = operator_account(db)
                if not account:
                    account = User(email="workspace@repopilot.local", password_hash=password_hash(os.urandom(32).hex()))
            else:
                # Public visitors get a private browser session, never the
                # operator's repositories, history, credentials, or approvals.
                account = User(email=f"guest-{uuid4()}@repopilot.local", password_hash=password_hash(os.urandom(32).hex()))
            db.add(account)
            db.flush()
        result = sign_in(response, account)
        if config.MODE == "demo" and not db.scalar(select(Repository).where(Repository.user_id == account.id)):
            db.add(Repository(user_id=account.id, full_name="demo/login-service", default_branch="main"))
        return result


@app.get(PREFIX + "/health")
def health():
    return {"status": "ok", "mode": config.MODE, "sandbox": "docker", "supported_stack": "Python + pytest"}


@app.get(PREFIX + "/repositories")
def repositories(account=Depends(user)):
    with Session() as db:
        return [
            {"id": r.id, "full_name": r.full_name, "default_branch": r.default_branch}
            for r in db.scalars(select(Repository).where(Repository.user_id == account))
            if config.MODE == "demo" or r.full_name != "demo/login-service"
        ]


@app.post(PREFIX + "/repositories", status_code=201)
def connect(body: RepoInput, account=Depends(user)):
    if config.MODE == "live":
        require_operator(account)
    try:
        metadata = GitHub().metadata(body.full_name)
    except Exception:
        raise HTTPException(
            422,
            "Repository unavailable. Check the server's GitHub access; demo mode supports demo/login-service.",
        )
    with Session.begin() as db:
        repo = db.scalar(
            select(Repository).where(Repository.user_id == account, Repository.full_name == body.full_name)
        )
        if not repo:
            repo = Repository(
                user_id=account,
                full_name=body.full_name,
                default_branch=metadata["default_branch"],
                github_repo_id=metadata["id"],
            )
            db.add(repo)
            db.flush()
        return {"id": repo.id, "full_name": repo.full_name, "default_branch": repo.default_branch}


@app.get(PREFIX + "/repositories/{repo_id}")
def repository(repo_id: str, account=Depends(user)):
    with Session() as db:
        repo = db.get(Repository, repo_id)
        if not repo or repo.user_id != account:
            raise HTTPException(404, "Repository not found")
        return {"id": repo.id, "full_name": repo.full_name, "default_branch": repo.default_branch}


@app.post(PREFIX + "/runs", status_code=202)
def create_run(body: RunInput, account=Depends(user)):
    if config.MODE == "live":
        require_operator(account)
    if config.MODE == "live" and not readiness()["ready"]:
        raise HTTPException(
            409,
            "Live connections are not ready. Open Live setup, save credentials, verify connections, and prepare the Docker sandbox.",
        )
    with Session.begin() as db:
        repo = db.get(Repository, body.repository_id)
        if not repo or repo.user_id != account:
            raise HTTPException(404, "Repository not found")
        if config.MODE == "live" and repo.full_name == "demo/login-service":
            raise HTTPException(422, "Connect a real GitHub repository in live mode")
        count = db.scalar(
            select(func.count())
            .select_from(Run)
            .where(Run.created_by == account, Run.status.in_(["queued", "running", "approval_queued"]))
        )
        if count >= 5:
            raise HTTPException(429, "At most five active runs are allowed")
        if config.MODE == "demo" and "email" not in body.issue_text.lower():
            raise HTTPException(
                422, "Demo mode supports the seeded missing-email issue. Switch to live mode for other tasks."
            )
        run = Run(
            created_by=account,
            repository_id=repo.id,
            issue_text=redact(body.issue_text),
            base_ref=body.base_ref,
            state={"mode": config.MODE},
        )
        db.add(run)
        db.flush()
        return {"id": run.id, "status": "queued", "message": "Agent run created"}


@app.get(PREFIX + "/runs")
def runs(account=Depends(user)):
    with Session() as db:
        return [
            run_view(r, db)
            for r in db.scalars(
                select(Run).where(Run.created_by == account).order_by(Run.created_at.desc()).limit(100)
            )
        ]


@app.get(PREFIX + "/runs/{run_id}")
def detail(run_id: str, account=Depends(user)):
    with Session() as db:
        return run_view(owned_run(db, run_id, account), db)


@app.get(PREFIX + "/runs/{run_id}/steps")
def steps(run_id: str, account=Depends(user)):
    with Session() as db:
        owned_run(db, run_id, account)
        return [
            {
                "id": s.id,
                "agent_name": s.agent_name,
                "output": s.output,
                "duration_ms": s.duration_ms,
                "created_at": s.created_at,
            }
            for s in db.scalars(select(Step).where(Step.run_id == run_id).order_by(Step.created_at))
        ]


@app.get(PREFIX + "/runs/{run_id}/artifacts")
def artifacts(run_id: str, account=Depends(user)):
    with Session() as db:
        owned_run(db, run_id, account)
        return [
            {
                "id": a.id,
                "artifact_type": a.artifact_type,
                "sha256": a.sha256,
                "download_url": f"{PREFIX}/runs/{run_id}/artifacts/{a.id}",
            }
            for a in db.scalars(select(Artifact).where(Artifact.run_id == run_id))
        ]


@app.get(PREFIX + "/runs/{run_id}/artifacts/{artifact_id}")
def download(run_id: str, artifact_id: str, account=Depends(user)):
    with Session() as db:
        owned_run(db, run_id, account)
        artifact = db.get(Artifact, artifact_id)
        if not artifact or artifact.run_id != run_id:
            raise HTTPException(404, "Artifact not found")
        return Response(
            artifact.content,
            media_type="text/plain",
            headers={
                "Content-Disposition": f'attachment; filename="{artifact.artifact_type}-{artifact.id}.txt"'
            },
        )


@app.get(PREFIX + "/runs/{run_id}/test-results")
def tests(run_id: str, account=Depends(user)):
    with Session() as db:
        owned_run(db, run_id, account)
        return [t.result for t in db.scalars(select(TestResult).where(TestResult.run_id == run_id))]


@app.post(PREFIX + "/runs/{run_id}/cancel")
def cancel(run_id: str, account=Depends(user)):
    with Session.begin() as db:
        run = owned_run(db, run_id, account)
        if run.status in {"completed", "failed", "rejected"}:
            raise HTTPException(409, "Run is already finished")
        if run.status in {"approval_queued"} or db.get(Approval, run_id):
            raise HTTPException(409, "Approval has already been committed; publishing may be in progress")
        result = db.execute(
            update(Run)
            .where(Run.id == run_id, Run.status.in_(["queued", "running", "awaiting_approval"]))
            .values(status="cancelled", finished_at=now())
        )
        if result.rowcount != 1 and run.status != "cancelled":
            raise HTTPException(409, "Run state changed; refresh before cancelling")
        return {"status": "cancelled", "message": "Cancellation takes effect at the next workflow boundary"}


@app.post(PREFIX + "/runs/{run_id}/approval")
def approve(run_id: str, body: Decision, account=Depends(user)):
    with Session.begin() as db:
        run = owned_run(db, run_id, account)
        existing = db.get(Approval, run_id)
        if existing:
            if existing.decision != body.decision:
                raise HTTPException(409, "A different decision is already recorded")
            return {"decision": existing.decision, "status": run.status}
        if run.status != "awaiting_approval":
            raise HTTPException(409, "Run is not awaiting approval")
        result = db.execute(
            update(Run)
            .where(Run.id == run_id, Run.status == "awaiting_approval")
            .values(status="approval_queued")
        )
        if result.rowcount != 1:
            raise HTTPException(409, "Run state changed; refresh and retry")
        db.add(
            Approval(run_id=run_id, reviewer_id=account, decision=body.decision, comment=redact(body.comment))
        )
        return {"decision": body.decision, "status": "approval_queued"}


@app.get(PREFIX + "/runs/{run_id}/pull-request")
def pull_request(run_id: str, account=Depends(user)):
    with Session() as db:
        owned_run(db, run_id, account)
        pr = db.get(PullRequest, run_id)
        return {"url": pr.url, "number": pr.number, "branch": pr.branch} if pr else None


@app.get(PREFIX + "/settings/readiness")
def live_readiness(account=Depends(user)):
    with Session() as db:
        operator = operator_account(db)
    return {**readiness(), "can_configure": bool(operator and operator.id == account)}


def require_operator(account):
    with Session() as db:
        operator = operator_account(db)
        if not operator or operator.id != account:
            raise HTTPException(403, "Only the local workspace owner can configure server connections")


@app.post(PREFIX + "/settings/connections")
def configure_connections(body: ConnectionInput, request: Request, account=Depends(user)):
    require_operator(account)
    with Session() as db:
        active = db.scalar(
            select(func.count())
            .select_from(Run)
            .where(Run.status.in_(["running", "queued", "approval_queued", "awaiting_approval"]))
        )
    if active:
        raise HTTPException(409, "Finish or cancel pending workflows before changing their connections")
    with Session() as db:
        owner = db.get(User, account)
        if owner.email.startswith("demo-") and owner.email.endswith("@repopilot.local") and not local_workspace_request(request):
            raise HTTPException(409, "Create a permanent account in Live setup before saving credentials")
    save_connections(body)
    return {
        "saved": True,
        "message": "Connections saved encrypted on this computer. Credentials are never returned.",
    }


@app.post(PREFIX + "/settings/verify")
def verify_connections(account=Depends(user)):
    require_operator(account)
    if config.MODE != "live":
        raise HTTPException(409, "Start RepoPilot in live mode to verify real connections")
    import httpx

    values = settings()
    results = {"verified_at": now()}
    ai = {"verified": False, "message": "Configure both the API key and model ID first."}
    if values["llm_api_key"] and values["model"]:
        try:
            ModelProvider().structured(
                Plan,
                "This is a connection test. Produce a short plan, one acceptance criterion, one step and no risks.",
                {
                    "issue": "Handle missing input without a server error. This is a harmless connection check; no repository content is sent."
                },
                max_tokens=800,
            )
            ai = {"verified": True, "message": "A real structured AI response was received and validated."}
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            ai["message"] = (
                "Model access denied. Check the API key and model ID."
                if code in {401, 403, 404}
                else "The model provider rejected the request. Check billing, rate limits, and JSON chat compatibility."
            )
        except Exception:
            ai["message"] = (
                "The model connection failed. Check the endpoint, network access, model ID, and JSON response support."
            )
    github = {"verified": False, "message": "Configure the GitHub token first."}
    if values["github_token"]:
        try:
            GitHub().request("GET", "/user")
            github = {
                "verified": True,
                "message": "GitHub authenticated the configured credential. Repository access is checked when you connect one.",
            }
        except Exception:
            github["message"] = "GitHub authentication failed. Check the token, expiry, and network access."
    results.update(ai=ai, github=github)
    try:
        record_verification(results, fingerprint(values))
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return readiness()


def operator_account(db):
    permanent = db.scalar(
        select(User).where(~User.email.like("demo-%@repopilot.local"), ~User.email.like("guest-%@repopilot.local")).order_by(User.created_at).limit(1)
    )
    return permanent or db.scalar(select(User).where(~User.email.like("guest-%@repopilot.local")).order_by(User.created_at).limit(1))


@app.post(PREFIX + "/auth/claim")
def claim_workspace(body: Credentials, response: Response, account=Depends(user)):
    require_operator(account)
    with Session.begin() as db:
        owner = db.get(User, account)
        if not (owner.email.startswith("demo-") and owner.email.endswith("@repopilot.local")):
            raise HTTPException(409, "This workspace already has a permanent account")
        owner.email = body.email.lower()
        owner.password_hash = password_hash(body.password)
        try:
            db.flush()
        except IntegrityError:
            raise HTTPException(409, "That email already has an account; sign in with it")
        return sign_in(response, owner)


@app.exception_handler(RequestValidationError)
async def private_validation_error(request, exc):
    # FastAPI's default errors can echo submitted password/token values.
    return JSONResponse(
        status_code=422,
        content={
            "detail": "Check the submitted fields. Secret values are never returned in validation errors."
        },
    )
