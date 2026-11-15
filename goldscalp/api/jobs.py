"""Background job runner shared by performance evaluation and research runs."""
from __future__ import annotations

import threading
from typing import Any


class JobRunner:
    """At most ONE heavy job (performance evaluation, backtest, comparison, walk-forward) runs at a time: they
    are CPU-bound and share the process with the server. Jobs are daemon threads with a cancel event."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running: str | None = None
        self._cancel: dict[str, threading.Event] = {}

    def busy(self) -> str | None:
        with self._lock:
            return self._running

    def start(self, run_id: str, target: Any) -> None:
        with self._lock:
            if self._running is not None:
                raise RuntimeError(self._running)
            self._running = run_id
            event = self._cancel[run_id] = threading.Event()

        def wrapper() -> None:
            try:
                target(event)
            finally:
                with self._lock:
                    self._running = None
                    self._cancel.pop(run_id, None)

        threading.Thread(target=wrapper, name=f"perf-{run_id}", daemon=True).start()

    def cancel(self, run_id: str) -> bool:
        with self._lock:
            event = self._cancel.get(run_id)
        if event is None:
            return False
        event.set()
        return True

    def cancel_all(self) -> None:
        with self._lock:
            events = list(self._cancel.values())
        for event in events:
            event.set()
