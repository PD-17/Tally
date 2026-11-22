"""Live events for the screens: the wallet and the runner publish, each open page subscribes."""

from __future__ import annotations

import asyncio
import json
import time


class EventHub:
    def __init__(self, history: int = 500):
        self._subscribers: set[asyncio.Queue] = set()
        self._recent: list[dict] = []  # so a page opened mid-run can catch up
        self._history = history

    def publish(self, run_id: str, kind: str, data: dict) -> None:
        """Safe to call from synchronous code (the wallet's callbacks)."""
        event = {"run_id": run_id, "kind": kind, "at": time.time(), **data}
        self._recent = (self._recent + [event])[-self._history:]
        for queue in list(self._subscribers):
            queue.put_nowait(event)

    def sink(self, run_id: str):
        """A callback for one run: `Run(on_event=hub.sink(run_id))`."""
        return lambda kind, data: self.publish(run_id, kind, data)

    async def stream(self, run_id: str | None = None):
        """Every event so far, then each new one, as server-sent-event payloads."""
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.add(queue)
        try:
            for event in list(self._recent):
                if run_id is None or event["run_id"].startswith(run_id):
                    yield {"data": json.dumps(event, default=str)}
            while True:
                event = await queue.get()
                if run_id is None or event["run_id"].startswith(run_id):
                    yield {"data": json.dumps(event, default=str)}
        finally:
            self._subscribers.discard(queue)
