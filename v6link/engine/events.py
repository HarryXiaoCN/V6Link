"""Event bus for engine -> UI notifications."""

from __future__ import annotations

from typing import Any

from .models import (
    EventSink,
    log,
    _monotonic,
)


class EventBus:
    """Owns the event sink and per-contact throttling state."""

    def __init__(self, sink: EventSink | None = None):
        self._sink: EventSink = sink or (lambda _payload: None)
        self._last_contact_event: dict[str, float] = {}

    @property
    def sink(self) -> EventSink:
        return self._sink

    def emit(self, event_type: str, **fields: Any) -> None:
        payload = {"type": event_type, **fields}
        try:
            self._sink(payload)
        except Exception:
            log.debug("event sink failed", exc_info=True)

    def emit_contact(self, contact: dict[str, Any], *, force: bool = False) -> None:
        contact_id = contact.get("id", "")
        now = _monotonic()
        if not force and now - self._last_contact_event.get(contact_id, 0.0) < 1.5:
            return
        self._last_contact_event[contact_id] = now
        self.emit("contact_updated", contact=contact)

    def emit_error(self, message: str, *, scope: str = "general") -> None:
        self.emit("error", scope=scope, message=message)
