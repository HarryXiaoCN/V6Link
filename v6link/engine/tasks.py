"""Background task registry shared by the engine services."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any

from .models import log


class TaskRegistry:
    """Creates, tracks and cancels the engine's background tasks."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task] = set()

    def spawn(self, awaitable: Awaitable[Any], *, name: str = "") -> asyncio.Task:
        task = asyncio.create_task(awaitable, name=name or None)
        self._tasks.add(task)

        def _done(finished: asyncio.Task) -> None:
            self._tasks.discard(finished)
            if finished.cancelled():
                return
            try:
                exc = finished.exception()
            except Exception:
                exc = None
            if exc is not None:
                log.debug("background task failed: %s", exc, exc_info=exc)

        task.add_done_callback(_done)
        return task

    def track(self, task: asyncio.Task) -> None:
        """Track an externally created task (for example the current handler)."""
        self._tasks.add(task)

    def untrack(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)

    async def cancel_all(self) -> None:
        current = asyncio.current_task()
        tasks = [task for task in self._tasks if task is not current and not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)