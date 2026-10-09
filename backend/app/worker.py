import time
from sqlalchemy import select, update
from langgraph.types import Command
from .db import Session, Run, Repository, now
from .workflow import durable_graph
from .security import redact


def execute(graph, run_id):
    cfg = {"configurable": {"thread_id": run_id}, "recursion_limit": 60}
    try:
        snapshot = graph.get_state(cfg)
        with Session() as db:
            run = db.get(Run, run_id)
            repo = db.get(Repository, run.repository_id)
            initial = {
                "run_id": run.id,
                "issue_text": run.issue_text,
                "repo_name": repo.full_name,
                "base_ref": run.base_ref,
            }
        if snapshot.tasks and any(t.interrupts for t in snapshot.tasks):
            graph.invoke(Command(resume=True), cfg)
        elif snapshot.next:
            graph.invoke(None, cfg)
        elif snapshot.values:
            # A completed checkpoint needs no replay of external effects.
            with Session.begin() as db:
                run = db.get(Run, run_id)
                run.status = snapshot.values.get("status", "completed")
                run.finished_at = now()
        else:
            graph.invoke(initial, cfg)
    except Exception as exc:
        message = (
            redact(str(exc))[:2000]
            if isinstance(exc, (ValueError, RuntimeError))
            else "An integration failed. Verify model/GitHub configuration, Docker availability and repository compatibility; saved artifacts remain available."
        )
        with Session.begin() as db:
            run = db.get(Run, run_id)
            if run and run.status != "cancelled":
                run.status = "failed"
                run.error_message = message
                run.finished_at = now()


def claim():
    with Session.begin() as db:
        run = db.scalar(
            select(Run).where(Run.status.in_(["queued", "approval_queued"])).order_by(Run.created_at).limit(1)
        )
        if not run:
            return None
        old = run.status
        result = db.execute(
            update(Run)
            .where(Run.id == run.id, Run.status == old)
            .values(status="running", started_at=run.started_at or now())
        )
        return run.id if result.rowcount == 1 else None


def main():
    # MVP supports a single worker. Restart resumes the last durable checkpoint.
    with Session.begin() as db:
        db.execute(update(Run).where(Run.status == "running").values(status="queued"))
    with durable_graph() as graph:
        while True:
            run_id = claim()
            if run_id:
                execute(graph, run_id)
            else:
                time.sleep(0.5)


if __name__ == "__main__":
    main()
