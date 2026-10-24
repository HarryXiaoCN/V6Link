"""Constants, shared helpers and transfer state for the networking engine."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import ipaddress
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import __version__
from ..crypto import (
    EncryptedSession,
)
from ..errors import (
    HandshakeError,
    IdentityMismatch,
    PeerApprovalRequired,
    PeerUnreachable,
    TransferError,
    UpdateError,
)
from ..filestore import MAX_CHUNK, MIN_CHUNK, AttachmentWriter

log = logging.getLogger("v6link.engine")

APP_WIRE_NAME = "V6Link"
APP_VERSION = __version__
PROTOCOL = 2
CONNECT_TIMEOUT = 4.0
CONTROL_TIMEOUT = 15.0
ACK_TIMEOUT = 30.0
FILE_CONSENT_TIMEOUT = 120.0
FILE_OFFER_TIMEOUT = 150.0
FILE_RESULT_TIMEOUT = 180.0
UPDATE_OFFER_TTL = 600.0
UPDATE_DATA_TIMEOUT = 60.0
MAX_TEXT_BYTES = 256 * 1024
MAX_FILE_BYTES = 1024 * 1024 * 1024 * 1024
MAX_STREAMS = 8
MAX_ACTIVE_INBOUND = 6
DISK_RESERVE = 256 * 1024 * 1024

EventSink = Callable[[dict[str, Any]], None]


def _now() -> float:
    return time.time()


def _monotonic() -> float:
    return time.monotonic()


def _clean_peer_name(value: Any) -> str:
    text = str(value or "")
    text = "".join(ch if ch.isprintable() and ch not in "\t\r\n" else " " for ch in text)
    text = " ".join(text.split())
    return text[:40] or "新联系人"


def _valid_announced_hosts(value: Any) -> list[str]:
    """Only accept routable IPv6 addresses advertised in an encrypted hello."""
    if not isinstance(value, (list, tuple)):
        return []
    hosts: list[str] = []
    for raw in list(value)[:12]:
        host = str(raw or "").strip()
        if not host or len(host) > 128:
            continue
        try:
            parsed = ipaddress.IPv6Address(host.split("%", 1)[0])
        except ValueError:
            continue
        if (
            parsed.is_loopback
            or parsed.is_link_local
            or parsed.is_multicast
            or parsed.is_unspecified
        ):
            continue
        if host not in hosts:
            hosts.append(host)
    return hosts


def _validate_file_id(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text or len(text) > 64 or any(ch not in "0123456789abcdef" for ch in text):
        raise TransferError("文件传输标识无效")
    return text


def _validate_port(value: Any) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise HandshakeError("端口字段无效") from exc
    if not 1 <= port <= 65535:
        raise HandshakeError("端口字段超出范围")
    return port


def _file_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _decode_have_bitmap(value: Any, total_chunks: int) -> set[int]:
    if value in (None, ""):
        return set()
    try:
        raw = base64.b64decode(str(value).encode("ascii"), validate=True)
    except Exception as exc:
        raise TransferError("续传位图格式无效") from exc
    expected = (total_chunks + 7) // 8
    if len(raw) != expected:
        raise TransferError("续传位图长度与文件分块数不匹配")
    received: set[int] = set()
    for byte_index, current in enumerate(raw):
        for bit_index in range(8):
            if current & (1 << bit_index):
                index = byte_index * 8 + bit_index
                if index >= total_chunks:
                    raise TransferError("续传位图包含越界分块")
                received.add(index)
    return received


def _received_bytes_for(size: int, chunk_size: int, received: set[int]) -> int:
    return sum(
        min(chunk_size, size - index * chunk_size)
        for index in received
        if 0 <= index < (size + chunk_size - 1) // chunk_size
    )


class _FileControlChannel:
    """The only reader for a file control session.

    Reading continuously is required so a peer can cancel or pause while the
    local side is waiting for consent or busy receiving data chunks.
    """

    def __init__(self, session: EncryptedSession, label: str):
        self.session = session
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.closed = asyncio.Event()
        self.cancel_event = asyncio.Event()
        self.cancel_reason = ""
        self._task = asyncio.create_task(self._read_loop(), name=f"file-control-{label}")

    async def _read_loop(self) -> None:
        error = ""
        try:
            while True:
                message = await self.session.receive_control()
                if not isinstance(message, dict):
                    raise TransferError("文件控制消息格式无效")
                if message.get("type") == "file_cancel":
                    self.cancel_reason = str(message.get("error") or "对方取消了文件传输")
                    self.cancel_event.set()
                await self.queue.put(message)
        except asyncio.CancelledError:
            error = "文件控制通道已关闭"
            raise
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            error = "文件控制连接已断开"
        except Exception as exc:
            error = _friendly_error(exc)
        finally:
            self.closed.set()
            with contextlib.suppress(Exception):
                self.queue.put_nowait(
                    {"type": "_control_closed", "error": error or "文件控制连接已关闭"}
                )

    async def receive(self, timeout: float | None = None) -> dict[str, Any]:
        if timeout is None:
            message = await self.queue.get()
        else:
            message = await asyncio.wait_for(self.queue.get(), timeout)
        if message.get("type") == "_control_closed":
            raise TransferError(str(message.get("error") or "文件控制连接已关闭"))
        return message

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self.session.close()
        if not self._task.done():
            self._task.cancel()
        with contextlib.suppress(BaseException):
            await self._task


@dataclass
class _PendingFileOffer:
    file_id: str
    contact_id: str
    contact_name: str
    peer_id: str
    name: str
    size: int
    chunk_size: int
    expected_streams: int
    fingerprint: str
    received_bytes: int
    have_bitmap: str
    writer: AttachmentWriter | None = None
    message: dict[str, Any] | None = None
    channel: _FileControlChannel | None = None
    decision_event: asyncio.Event = field(default_factory=asyncio.Event)
    accepted: bool | None = None
    reason: str = ""


@dataclass
class _OutboundFile:
    message_id: str
    file_id: str = ""
    control: EncryptedSession | None = None
    channel: _FileControlChannel | None = None
    task: asyncio.Task | None = None


class _PermanentFileError(TransferError):
    """A file identity/local source error that resume cannot repair."""


def _validate_chunk_size(value: Any) -> int:
    try:
        chunk = int(value)
    except (TypeError, ValueError) as exc:
        raise TransferError("分块大小无效") from exc
    if not MIN_CHUNK <= chunk <= MAX_CHUNK:
        raise TransferError("分块大小超出允许范围")
    return chunk


class ProgressEmitter:
    """Throttled transfer progress events, at most ~12 per second."""

    def __init__(
        self,
        sink: EventSink,
        contact_id: str,
        message_id: str,
        direction: str,
        total: int,
        *,
        interval: float = 0.08,
    ):
        self.sink = sink
        self.contact_id = contact_id
        self.message_id = message_id
        self.direction = direction
        self.total = max(0, int(total))
        self.interval = interval
        self.started = _monotonic()
        self.last_emit = 0.0
        self.last_bytes = 0
        self.last_time = self.started

    def update(self, done: int, *, force: bool = False) -> None:
        done = max(0, min(int(done), self.total))
        now = _monotonic()
        if not force and now - self.last_emit < self.interval and done < self.total:
            return
        elapsed = max(0.001, now - self.started)
        speed = done / elapsed
        self.last_emit = now
        self.last_bytes = done
        self.last_time = now
        payload = {
            "type": "transfer_progress",
            "contact_id": self.contact_id,
            "message_id": self.message_id,
            "direction": self.direction,
            "transferred": done,
            "total": self.total,
            "speed": speed,
            "percent": (done / self.total * 100.0) if self.total else 100.0,
        }
        try:
            self.sink(payload)
        except Exception:
            log.debug("event sink raised", exc_info=True)

    def finish(self) -> None:
        self.update(self.total, force=True)


class _InboundTransfer:
    def __init__(
        self,
        *,
        file_id: str,
        peer_id: str,
        contact_id: str,
        message_id: str,
        writer: AttachmentWriter,
        size: int,
        chunk_size: int,
        expected_streams: int,
        progress: ProgressEmitter,
    ):
        self.file_id = file_id
        self.peer_id = peer_id
        self.contact_id = contact_id
        self.message_id = message_id
        self.writer = writer
        self.size = size
        self.chunk_size = chunk_size
        self.expected_chunks = writer.expected_chunks
        self.expected_streams = expected_streams
        self.progress = progress
        self.started = _monotonic()
        self.last_activity = self.started
        self.sender_finished = False
        self.error: str | None = None
        self.finalized = False
        self.progress_event = asyncio.Event()
        self.data_streams = 0
        self.max_streams_seen = 0
        self.control: EncryptedSession | None = None
        self.channel: _FileControlChannel | None = None
        self.data_sessions: set[EncryptedSession] = set()
        self.canceled = False

    @property
    def received_chunks(self) -> int:
        return self.writer.received_chunks

    @property
    def received_bytes(self) -> int:
        return self.writer.received_bytes

    def mark_activity(self) -> None:
        self.last_activity = _monotonic()
        self.progress_event.set()

    def fail(self, message: str) -> None:
        if not self.error:
            self.error = message
        self.progress_event.set()


def _read_range(handle, offset: int, length: int) -> bytes:
    handle.seek(offset)
    return handle.read(length)


def _parse_control_payload(body: bytes) -> dict[str, Any]:
    import json

    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise TransferError("控制帧 JSON 无效") from exc
    if not isinstance(value, dict) or not isinstance(value.get("type"), str):
        raise TransferError("控制帧缺少类型")
    return value


def _friendly_error(exc: BaseException) -> str:
    if isinstance(
        exc,
        (
            PeerUnreachable,
            PeerApprovalRequired,
            IdentityMismatch,
            HandshakeError,
            TransferError,
            UpdateError,
        ),
    ):
        return str(exc)
    if isinstance(exc, asyncio.CancelledError):
        return "操作已取消"
    if isinstance(exc, asyncio.TimeoutError):
        return "操作超时"
    if isinstance(exc, (ConnectionError, BrokenPipeError)):
        return "加密连接已中断"
    if isinstance(exc, OSError):
        return str(exc.strerror or exc)
    text = str(exc).strip()
    return text or exc.__class__.__name__
