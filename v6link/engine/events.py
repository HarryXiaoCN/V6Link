"""Event emission helpers."""

from __future__ import annotations

from typing import Any


from .models import (
    log,
    _monotonic,
)


class _EventMixin:
    """Event emission helpers."""

    def _emit(self, event_type: str, **fields: Any) -> None:
        payload = {"type": event_type, **fields}
        try:
            self._event_sink(payload)
        except Exception:
            log.debug("event sink failed", exc_info=True)

    def _emit_contact(self, contact: dict[str, Any], *, force: bool = False) -> None:
        contact_id = contact.get("id", "")
        now = _monotonic()
        if not force and now - self._last_contact_event.get(contact_id, 0.0) < 1.5:
            return
        self._last_contact_event[contact_id] = now
        self._emit("contact_updated", contact=contact)

    def _emit_error(self, message: str, *, scope: str = "general") -> None:
        self._emit("error", scope=scope, message=message)
