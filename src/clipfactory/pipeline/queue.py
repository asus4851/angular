"""Job queue: enqueue, atomically claim, complete/fail with retry backoff.

The queue lives entirely in the `jobs` table (see docs/ARCHITECTURE.md §4).
`claim_next` is the only place that mutates a job from QUEUED to RUNNING; it
does so with a conditional UPDATE so that concurrent callers sharing the same
SQLite database never claim the same row twice.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import update
from sqlalchemy.orm import Session

from clipfactory.config import get_settings
from clipfactory.models import Job, JobStatus, JobType, utcnow

logger = logging.getLogger(__name__)

_MAX_ERROR_LEN = 2000
_BACKOFF_BASE_SEC = 30


def enqueue(
    session: Session,
    type: JobType,
    payload: dict,
    run_at: datetime | None = None,
    dedupe: bool = True,
) -> Job:
    """Insert a new job, or return a matching already-queued/running one.

    Dedup compares `payload` for equality against other QUEUED/RUNNING jobs of
    the same `type` (a plain Python dict comparison of the JSON payload).
    """
    if dedupe:
        existing_jobs = (
            session.query(Job)
            .filter(Job.type == type, Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]))
            .all()
        )
        for job in existing_jobs:
            if job.payload == payload:
                return job

    job = Job(
        type=type,
        payload=payload,
        run_at=run_at or utcnow(),
        max_attempts=get_settings().job_max_attempts,
    )
    session.add(job)
    session.flush()
    logger.info("enqueue: %s %s -> job %d", type.value, payload, job.id)
    return job


def claim_next(session: Session) -> Job | None:
    """Atomically claim the oldest due QUEUED job, or return None if there is none."""
    now = utcnow()
    candidate = (
        session.query(Job)
        .filter(Job.status == JobStatus.QUEUED, Job.run_at <= now)
        .order_by(Job.run_at)
        .first()
    )
    if candidate is None:
        return None

    result = session.execute(
        update(Job).where(Job.id == candidate.id, Job.status == JobStatus.QUEUED).values(status=JobStatus.RUNNING)
    )
    if result.rowcount == 0:
        # Another worker claimed it between our SELECT and UPDATE.
        return None

    session.refresh(candidate)
    logger.info("claim_next: claimed job %d (%s)", candidate.id, candidate.type.value)
    return candidate


def complete(session: Session, job: Job) -> None:
    """Mark a job DONE."""
    job.status = JobStatus.DONE
    session.add(job)


def fail(session: Session, job: Job, error: str) -> None:
    """Apply the retry policy: requeue with exponential backoff, or give up."""
    job.attempts += 1
    job.last_error = error[:_MAX_ERROR_LEN]
    if job.attempts < job.max_attempts:
        job.status = JobStatus.QUEUED
        job.run_at = utcnow() + timedelta(seconds=_BACKOFF_BASE_SEC * (2**job.attempts))
    else:
        job.status = JobStatus.FAILED
    session.add(job)
