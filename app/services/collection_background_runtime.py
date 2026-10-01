"""Bounded application-owned background loop for scheduling and collection."""

from __future__ import annotations

import logging
import threading

from app.services.collection_job_worker import CollectionJobWorker, WorkerResult
from app.services.collection_scheduler import CollectionScheduler


logger = logging.getLogger("publicdb2")


class CollectionBackgroundRuntime:
    def __init__(
        self,
        worker: CollectionJobWorker,
        scheduler: CollectionScheduler,
        *,
        idle_seconds: float = 2.0,
    ) -> None:
        self.worker = worker
        self.scheduler = scheduler
        self.idle_seconds = idle_seconds
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="publicdb2-collection-worker", daemon=True)
        self._thread.start()

    def notify(self) -> None:
        self._wake.set()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=max(0.0, timeout))

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.scheduler.tick()
                if self._stop.is_set():
                    break
                result = self.worker.run_one()
                if result is WorkerResult.SYSTEM_FAILURE:
                    logger.error("background collection stopped after systemic failure")
                    break
                if result is WorkerResult.ITEM_COMPLETE:
                    continue
            except Exception:
                logger.exception("background scheduler stopped after systemic failure")
                break
            self._wake.wait(self.idle_seconds)
            self._wake.clear()
