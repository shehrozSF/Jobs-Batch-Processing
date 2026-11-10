"""Batch operations shared by the API and the scripts."""

from datetime import timedelta

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.db import DEAD, PAUSED, QUEUED, RUNNING, STATUSES, SUCCEEDED, Batch, Job, utcnow

# A job "running" for longer than this lost its worker (crash, Ctrl+C,
# container stop). Longer than the queue's 15-minute job timeout, so a job
# that is genuinely still running is never picked up twice.
STALE_AFTER = timedelta(minutes=20)


def _retryable(batch_id: int):
    """Jobs a retry should send back through: dead, paused, or stranded."""
    return (Job.batch_id == batch_id) & or_(
        Job.status.in_((DEAD, PAUSED)),
        (Job.status == RUNNING) & (Job.updated_at < utcnow() - STALE_AFTER),
    )


def create_batch(session: Session, name: str, postings: list[tuple[str, str]]) -> Batch:
    """postings: (source_key, body) pairs. Returns the batch with its jobs queued."""
    if not postings:
        raise ValueError("a batch needs at least one posting")
    keys = [key for key, _ in postings]
    if len(set(keys)) != len(keys):
        raise ValueError("source keys must be unique within a batch")
    if any(not body.strip() for _, body in postings):
        raise ValueError("posting bodies must not be empty")

    batch = Batch(name=name, jobs=[Job(source_key=k, body=b) for k, b in postings])
    session.add(batch)
    session.commit()
    return batch


def job_ids(session: Session, batch_id: int, status: str = QUEUED) -> list[int]:
    return list(session.scalars(
        select(Job.id).where(Job.batch_id == batch_id, Job.status == status).order_by(Job.id)
    ))


def progress(session: Session, batch_id: int) -> dict:
    batch = session.get(Batch, batch_id)
    if batch is None:
        raise LookupError(f"batch {batch_id} not found")

    counts = dict.fromkeys(STATUSES, 0)
    counts.update(session.execute(
        select(Job.status, func.count()).where(Job.batch_id == batch_id).group_by(Job.status)
    ).all())
    calls, tokens_in, tokens_out, cost = session.execute(
        select(
            func.coalesce(func.sum(Job.model_calls), 0),
            func.coalesce(func.sum(Job.input_tokens), 0),
            func.coalesce(func.sum(Job.output_tokens), 0),
            func.coalesce(func.sum(Job.cost_usd), 0.0),
        ).where(Job.batch_id == batch_id)
    ).one()

    retryable = session.scalar(select(func.count()).where(_retryable(batch_id)))
    total = sum(counts.values())
    finished = counts[SUCCEEDED] + counts[DEAD]
    return {
        "id": batch.id,
        "name": batch.name,
        "halted_reason": batch.halted_reason,
        "total": total,
        "counts": counts,
        "percent": round(100 * finished / total, 1) if total else 0.0,
        "complete": finished == total,
        "retryable": int(retryable),
        "model_calls": int(calls),
        "input_tokens": int(tokens_in),
        "output_tokens": int(tokens_out),
        "cost_usd": round(float(cost), 4),
    }


def requeue(session: Session, batch_id: int) -> list[int]:
    """Send every dead, paused and stranded job back through, and lift a halt.
    Use after fixing a prompt, the account's billing, or waiting out a quota."""
    if session.get(Batch, batch_id) is None:
        raise LookupError(f"batch {batch_id} not found")
    # UPDATE statements rather than attribute changes: they go straight to the
    # database, so a stale object in this session cannot swallow the change.
    ids = list(session.scalars(
        update(Job)
        .where(_retryable(batch_id))
        .values(status=QUEUED, error=None)
        .execution_options(synchronize_session=False)
        .returning(Job.id)
    ))
    session.execute(update(Batch).where(Batch.id == batch_id).values(halted_reason=None))
    session.commit()
    session.expire_all()
    return sorted(ids)
