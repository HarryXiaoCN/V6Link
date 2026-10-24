"""Shared mutable state for the file transfer services."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from ..models import _InboundTransfer, _OutboundFile, _PendingFileOffer


@dataclass
class FileState:
    """Per-engine transfer state shared by the file services."""

    active_inbound: dict[str, _InboundTransfer] = field(default_factory=dict)
    pending_offers: dict[str, _PendingFileOffer] = field(default_factory=dict)
    outbound_files: dict[str, _OutboundFile] = field(default_factory=dict)
    file_locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    downloads_dir: Path = field(default_factory=lambda: Path.home() / "Downloads")
    download_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    stopping: bool = False