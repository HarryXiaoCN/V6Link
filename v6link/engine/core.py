"""The composed ChatEngine class."""

from __future__ import annotations

import asyncio
from pathlib import Path

from ..crypto import (
    EncryptedSession,
    Identity,
)
from ..vault import Vault
from .connections import _ConnectionMixin
from .contacts import _ContactMixin
from .events import _EventMixin
from .exports import _ExportMixin
from .inbound_files import _InboundFileMixin
from .lifecycle import _LifecycleMixin
from .messaging import _MessagingMixin
from .models import (
    APP_VERSION,
    EventSink,
    _InboundTransfer,
    _OutboundFile,
    _PendingFileOffer,
)
from .outbound_files import _OutboundFileMixin
from .updates import _IncomingUpdate, _OutgoingUpdate, _UpdateMixin


class ChatEngine(
    _EventMixin,
    _LifecycleMixin,
    _ConnectionMixin,
    _MessagingMixin,
    _ContactMixin,
    _InboundFileMixin,
    _OutboundFileMixin,
    _UpdateMixin,
    _ExportMixin,
):
    """Owns the IPv6 listener, encrypted sessions, and outgoing transfers."""

    def __init__(
        self,
        vault: Vault,
        event_sink: EventSink | None = None,
        *,
        bind_host: str = "::",
        listen_port: int | None = None,
        downloads_dir: str | Path | None = None,
        version: str = APP_VERSION,
    ):
        self.vault = vault
        self.identity: Identity = vault.identity()
        self.version = str(version or APP_VERSION)
        self.bind_host = bind_host
        profile = vault.profile()
        settings = vault.settings()
        requested = listen_port if listen_port is not None else profile.get("listen_port", 4455)
        self.port = int(requested)
        self._requested_port = int(profile.get("listen_port", 4455))
        self._event_sink = event_sink or (lambda _payload: None)
        if downloads_dir is not None:
            self._downloads_dir = Path(downloads_dir).expanduser()
        else:
            configured_downloads = str(settings.get("downloads_dir", "") or "").strip()
            self._downloads_dir = (
                Path(configured_downloads).expanduser()
                if configured_downloads
                else Path.home() / "Downloads"
            )
        self._download_lock = asyncio.Lock()
        self._server: asyncio.AbstractServer | None = None
        self._sessions: set[EncryptedSession] = set()
        self._tasks: set[asyncio.Task] = set()
        self._active_inbound: dict[str, _InboundTransfer] = {}
        self._pending_offers: dict[str, _PendingFileOffer] = {}
        self._outbound_files: dict[str, _OutboundFile] = {}
        self._outgoing_updates: dict[str, _OutgoingUpdate] = {}
        self._incoming_updates: dict[str, _IncomingUpdate] = {}
        self._file_locks: dict[str, asyncio.Lock] = {}
        self._started = False
        self._stopping = False
        self._last_contact_event: dict[str, float] = {}
        self._accepted_peer_ids: set[str] = set()
        self._online_window = 120.0
