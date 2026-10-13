"""Background jobs with progress, for work that takes seconds or minutes (OCR, exports).

The UI starts a job, then polls :meth:`Jobs.status` a few times per second and can
cancel it. A job function receives the :class:`Job` and reports through
``job.progress(done, total)``, which returns False once cancellation was requested.
"""
from __future__ import annotations

import itertools
import logging
import threading
import time

from .pdftools import Cancelled

log = logging.getLogger("aurora.jobs")


class Job:
    def __init__(self, ident: str, kind: str):
        self.id = ident
        self.kind = kind
        self.state = "running"  # running | done | error | cancelled
        self.done = 0
        self.total = 0
        self.result = None
        self.error = ""
        self.cancel_requested = False
        self.started = time.time()

    def progress(self, done: int, total: int) -> bool:
        self.done, self.total = int(done), int(total)
        return not self.cancel_requested

    def as_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "state": self.state, "done": self.done,
                "total": self.total, "result": self.result, "error": self.error}


class Jobs:
    def __init__(self):
        self._jobs: dict[str, Job] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def start(self, kind: str, fn) -> str:
        job = Job(f"j{next(self._ids)}", kind)
        with self._lock:
            self._jobs[job.id] = job
            # keep the registry small: forget finished jobs older than 10 minutes
            old = [k for k, j in self._jobs.items() if j.state != "running" and time.time() - j.started > 600]
            for k in old:
                del self._jobs[k]

        def run():
            try:
                job.result = fn(job)
                job.state = "cancelled" if job.cancel_requested else "done"
            except Cancelled:
                job.state = "cancelled"
            except Exception as exc:  # reported to the UI, full trace to the log
                log.exception("job %s (%s) failed", job.id, kind)
                job.state = "error"
                job.error = str(exc) or type(exc).__name__

        threading.Thread(target=run, name=f"job-{kind}", daemon=True).start()
        return job.id

    def status(self, ident: str) -> dict:
        job = self._jobs.get(ident)
        return job.as_dict() if job else {"id": ident, "state": "missing"}

    def cancel(self, ident: str) -> None:
        job = self._jobs.get(ident)
        if job:
            job.cancel_requested = True

    def running(self) -> list[dict]:
        return [j.as_dict() for j in self._jobs.values() if j.state == "running"]
