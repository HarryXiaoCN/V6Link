"""Queued bridge from the backend asyncio thread into Qt's main thread."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, Qt, Signal, Slot

log = logging.getLogger("v6link.ui.bridge")


class EventBridge(QObject):
    """Signals emitted from the backend are queued, never executed there."""

    _event = Signal(object)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._handler: Callable[[dict[str, Any]], None] | None = None
        self._backlog: list[dict[str, Any]] = []
        self._event.connect(self._deliver, Qt.QueuedConnection)

    def emit_event(self, payload: dict[str, Any]) -> None:
        self._event.emit(payload)

    @Slot(object)
    def _deliver(self, payload: dict[str, Any]) -> None:
        if self._handler is None:
            self._backlog.append(payload)
            return
        try:
            self._handler(payload)
        except Exception:
            log.exception("UI event handler failed")

    def attach(self, handler: Callable[[dict[str, Any]], None]) -> None:
        self._handler = handler
        backlog = self._backlog
        self._backlog = []
        for payload in backlog:
            try:
                handler(payload)
            except Exception:
                log.exception("UI backlog handler failed")

    def detach(self) -> None:
        self._handler = None
        self._backlog.clear()
