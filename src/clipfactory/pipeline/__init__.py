"""Pipeline orchestration: job queue, stage handlers, worker loop, scheduler.

See docs/ARCHITECTURE.md §4 for the job-type state machine.
"""

from __future__ import annotations

from clipfactory.pipeline.queue import claim_next, complete, enqueue, fail
from clipfactory.pipeline.scheduler import enqueue_due_polls, start_scheduler
from clipfactory.pipeline.stages import HANDLERS, approve_candidate
from clipfactory.pipeline.worker import drain_queue, run_worker, start_worker_thread

__all__ = [
    "enqueue",
    "claim_next",
    "complete",
    "fail",
    "HANDLERS",
    "approve_candidate",
    "run_worker",
    "start_worker_thread",
    "drain_queue",
    "enqueue_due_polls",
    "start_scheduler",
]
