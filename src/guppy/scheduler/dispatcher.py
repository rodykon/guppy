"""Bounded-concurrency dispatch of queued jobs to worker containers.

Launches up to `max_concurrent` jobs at once; the rest stay QUEUED in the
store until a slot frees up. If a worker container exits without ever
writing a final status (crash, OOM kill, etc.), the dispatcher treats that
as a failure itself rather than leaving the job stuck at RUNNING forever.
"""

from __future__ import annotations

import logging
from typing import Callable

from guppy.common.store import Job, JobStatus, Store
from guppy.scheduler.launcher import WorkerJobSpec, WorkerLauncher, WorkerPhase

logger = logging.getLogger(__name__)


class Dispatcher:
    def __init__(self, store: Store, launcher: WorkerLauncher, max_concurrent: int):
        self._store = store
        self._launcher = launcher
        self._max_concurrent = max_concurrent
        self._running: dict[str, str] = {}  # job_id -> launcher handle

    def reconcile_on_startup(self) -> None:
        discovered = self._launcher.discover_active()
        if discovered:
            logger.info("reattaching to %d worker(s) from a previous run", len(discovered))
        self._running.update(discovered)
        self.poll_running()

    def launch_ready_jobs(self, spec_builder: Callable[[Job], WorkerJobSpec]) -> None:
        capacity = self._max_concurrent - len(self._running)
        if capacity <= 0:
            return
        for job in self._store.get_queued_jobs()[:capacity]:
            try:
                spec = spec_builder(job)
                handle = self._launcher.launch(spec)
            except Exception as e:
                logger.exception("failed to launch worker for job %s", job.id)
                self._store.mark_failed(job.id, f"failed to launch worker: {e}")
                continue
            self._running[job.id] = handle
            self._store.mark_running(job.id)
            logger.info("launched job %s (%s#%d)", job.id, job.repo_slug, job.issue_number)

    def poll_running(self) -> None:
        finished_job_ids = []
        for job_id, handle in self._running.items():
            state = self._launcher.poll(handle)
            if state.phase == WorkerPhase.RUNNING:
                continue

            finished_job_ids.append(job_id)
            job = self._store.get_job(job_id)
            if job is not None and job.status == JobStatus.RUNNING:
                message = f"worker exited (code={state.exit_code}) without reporting a result"
                if state.logs_tail:
                    message += f"\n--- last container logs ---\n{state.logs_tail}"
                logger.warning("job %s: %s", job_id, message)
                self._store.mark_failed(job_id, message)
            self._launcher.cleanup(handle)

        for job_id in finished_job_ids:
            del self._running[job_id]
