"""Worker loop: claim a job, run its handler, apply the retry policy.

Each phase (claim / execute / complete-or-fail) runs in its own
`session_scope()` so that a handler crash never leaves the claiming
transaction (or a broken session) behind; only the job id/type/payload cross
session boundaries.
"""

from __future__ import annotations

import logging
import threading
import time

from clipfactory.db import session_scope
from clipfactory.models import Job
from clipfactory.pipeline import queue
from clipfactory.pipeline.stages import HANDLERS

logger = logging.getLogger(__name__)

_STOP_CHECK_INTERVAL_SEC = 0.5


def _run_once() -> bool:
    """Claim and run a single due job. Returns False if there was none to claim."""
    with session_scope() as session:
        job = queue.claim_next(session)
        if job is None:
            return False
        job_id, job_type, payload = job.id, job.type, job.payload

    try:
        with session_scope() as session:
            HANDLERS[job_type](session, payload)
    except Exception as exc:
        logger.exception("worker: job %d (%s) failed", job_id, job_type.value)
        with session_scope() as session:
            job = session.get(Job, job_id)
            queue.fail(session, job, str(exc))
    else:
        with session_scope() as session:
            job = session.get(Job, job_id)
            queue.complete(session, job)

    return True


def run_worker(stop_event: threading.Event, poll_interval: float | None = None) -> None:
    """Loop claiming and running due jobs until `stop_event` is set."""
    from clipfactory.config import get_settings

    interval = poll_interval if poll_interval is not None else get_settings().worker_poll_sec

    # Recover jobs a previous worker process left RUNNING when it crashed
    # (or was killed) mid-handler -- claim_next never re-selects RUNNING
    # jobs, so without this they'd be stuck forever.
    with session_scope() as session:
        requeued = queue.requeue_stale_running(session)
    if requeued:
        logger.info("worker: requeued %d stale RUNNING job(s) on startup", requeued)

    logger.info("worker: starting (poll_interval=%.1fs)", interval)

    while not stop_event.is_set():
        try:
            processed = _run_once()
        except Exception:
            logger.exception("worker: unexpected error claiming/running a job")
            processed = False

        if not processed:
            waited = 0.0
            while waited < interval and not stop_event.is_set():
                step = min(_STOP_CHECK_INTERVAL_SEC, interval - waited)
                time.sleep(max(step, 0.0))
                waited += step

    logger.info("worker: stopped")


def start_worker_thread(stop_event: threading.Event) -> threading.Thread:
    thread = threading.Thread(target=run_worker, args=(stop_event,), daemon=True, name="cf-worker")
    thread.start()
    return thread


def drain_queue(max_jobs: int = 50) -> int:
    """Synchronously run queued jobs until none remain (or `max_jobs` is hit).

    Used by the offline demo and by tests to run the pipeline to completion
    without spinning up a background thread.
    """
    processed = 0
    while processed < max_jobs and _run_once():
        processed += 1
    return processed
