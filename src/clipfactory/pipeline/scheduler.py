"""Periodic enqueue of due channel polls.

A plain loop thread is simpler to test and reason about than APScheduler for
a single periodic tick, so that's what's used here (see
docs/ARCHITECTURE.md §4-5); apscheduler stays an available dependency for
future, more elaborate scheduling needs.
"""

from __future__ import annotations

import logging
import threading
import time

from clipfactory.db import session_scope
from clipfactory.models import Channel, JobType, utcnow
from clipfactory.pipeline import queue

logger = logging.getLogger(__name__)

_LOOP_INTERVAL_SEC = 60
_STOP_CHECK_INTERVAL_SEC = 0.5


def enqueue_due_polls() -> int:
    """Enqueue POLL_CHANNEL for every enabled channel whose interval has elapsed."""
    count = 0
    with session_scope() as session:
        now = utcnow()
        channels = session.query(Channel).filter(Channel.enabled.is_(True)).all()
        for channel in channels:
            due = (
                channel.last_checked_at is None
                or (now - channel.last_checked_at).total_seconds() >= channel.check_interval_min * 60
            )
            if due:
                queue.enqueue(session, JobType.POLL_CHANNEL, {"channel_id": channel.id}, dedupe=True)
                count += 1
    return count


def start_scheduler(stop_event: threading.Event) -> threading.Thread:
    def _loop() -> None:
        logger.info("scheduler: starting (interval=%ds)", _LOOP_INTERVAL_SEC)
        while not stop_event.is_set():
            try:
                enqueue_due_polls()
            except Exception:
                logger.exception("scheduler: error enqueueing due polls")

            waited = 0.0
            while waited < _LOOP_INTERVAL_SEC and not stop_event.is_set():
                step = min(_STOP_CHECK_INTERVAL_SEC, _LOOP_INTERVAL_SEC - waited)
                time.sleep(max(step, 0.0))
                waited += step
        logger.info("scheduler: stopped")

    thread = threading.Thread(target=_loop, daemon=True, name="cf-scheduler")
    thread.start()
    return thread
