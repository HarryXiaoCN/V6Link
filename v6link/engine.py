"""Async networking engine: encrypted messaging and parallel file transfer."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import ipaddress
import logging
import shutil
import socket
import struct
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .crypto import (
    HANDSHAKE_TIMEOUT,
    EncryptedSession,
    Identity,
    client_handshake,
    server_handshake,
)
from .errors import (
    HandshakeError,
    IdentityMismatch,
    PeerApprovalRequired,
    PeerUnreachable,
    TransferError,
    V6LinkError,
)
from .filestore import DEFAULT_CHUNK, MAX_CHUNK, MIN_CHUNK, AttachmentWriter
from .netutil import (
    format_endpoint,
    human_size,
    sanitize_filename,
    shareable_ipv6_addresses,
)
from .vault import Vault

log = logging.getLogger("v6link.engine")

APP_WIRE_NAME = "V6Link"
PROTOCOL = 2
CONNECT_TIMEOUT = 4.0
CONTROL_TIMEOUT = 15.0
ACK_TIMEOUT = 30.0
FILE_CONSENT_TIMEOUT = 120.0
FILE_OFFER_TIMEOUT = 150.0
FILE_RESULT_TIMEOUT = 180.0
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


class ChatEngine:
    """Owns the IPv6 listener, encrypted sessions, and outgoing transfers."""

    def __init__(
        self,
        vault: Vault,
        event_sink: EventSink | None = None,
        *,
        bind_host: str = "::",
        listen_port: int | None = None,
        downloads_dir: str | Path | None = None,
    ):
        self.vault = vault
        self.identity: Identity = vault.identity()
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
        self._file_locks: dict[str, asyncio.Lock] = {}
        self._started = False
        self._stopping = False
        self._last_contact_event: dict[str, float] = {}
        self._accepted_peer_ids: set[str] = set()
        self._online_window = 120.0

    # ------------------------------------------------------------------ events

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

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        if self._started:
            return
        await self._start_listener(self.port)
        self._started = True
        self._emit(
            "network",
            listening=True,
            port=self.port,
            host=self.bind_host,
        )

    async def _start_listener(self, port: int) -> None:
        raw_socket = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            # On Windows SO_REUSEADDR would let another local process bind the
            # same port; exclusive use makes port conflicts fail closed.
            raw_socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            raw_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "IPV6_V6ONLY"):
            try:
                raw_socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            except OSError:
                pass
        raw_socket.setblocking(False)
        try:
            raw_socket.bind((self.bind_host, int(port)))
            raw_socket.listen(128)
        except Exception:
            raw_socket.close()
            raise
        try:
            self._server = await asyncio.start_server(
                self._handle_incoming_connection,
                sock=raw_socket,
                backlog=128,
            )
        except Exception:
            raw_socket.close()
            raise
        self.port = int(self._server.sockets[0].getsockname()[1])

    async def stop(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        server = self._server
        self._server = None
        if server is not None:
            server.close()
            with contextlib.suppress(Exception):
                await server.wait_closed()
        for transfer in list(self._active_inbound.values()):
            await self._pause_inbound(
                transfer,
                "应用正在关闭，传输已暂停，可继续",
                notify_peer=False,
            )
        for record in list(self._outbound_files.values()):
            try:
                current_message = self.vault.get_message(record.message_id)
                if current_message is not None and current_message.get("status") in (
                    "sending",
                    "awaiting_consent",
                    "transferring",
                ):
                    self.vault.update_message(
                        record.message_id,
                        status="paused",
                        error="应用正在关闭，传输已暂停，可继续",
                        file_update={
                    "paused_at": _now(),
                    "received_bytes": int(transfer.received_bytes),
                },
                    )
            except Exception:
                log.debug("failed to pause outgoing transfer on shutdown", exc_info=True)
        current = asyncio.current_task()
        tasks = [task for task in self._tasks if task is not current and not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for session in list(self._sessions):
            await session.close()
        self._sessions.clear()
        self._emit("network", listening=False, port=self.port, host=self.bind_host)
        self._started = False

    def _spawn(self, awaitable: Awaitable[Any], *, name: str = "") -> asyncio.Task:
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

    @property
    def is_running(self) -> bool:
        return self._started

    def _contact_online(self, contact: dict[str, Any]) -> bool:
        return _now() - float(contact.get("last_seen", 0.0) or 0.0) < self._online_window

    # --------------------------------------------------------- network helpers

    @staticmethod
    def _configure_socket(writer: asyncio.StreamWriter) -> None:
        sock = writer.get_extra_info("socket")
        if sock is None:
            return
        with contextlib.suppress(OSError, AttributeError):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        with contextlib.suppress(OSError, AttributeError):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        if hasattr(socket, "TCP_KEEPIDLE"):
            with contextlib.suppress(OSError):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 30)
        if hasattr(socket, "TCP_KEEPINTVL"):
            with contextlib.suppress(OSError):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
        if hasattr(socket, "TCP_KEEPCNT"):
            with contextlib.suppress(OSError):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)

    async def _connect(self, host: str, port: int) -> EncryptedSession:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, family=socket.AF_INET6),
            timeout=CONNECT_TIMEOUT,
        )
        try:
            self._configure_socket(writer)
            session = await client_handshake(reader, writer, self.identity)
        except BaseException:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            raise
        self._sessions.add(session)
        return session

    async def _close_session(self, session: EncryptedSession | None) -> None:
        if session is None:
            return
        self._sessions.discard(session)
        await session.close()

    @staticmethod
    def _source_address(writer: asyncio.StreamWriter) -> tuple[str, int]:
        peer = writer.get_extra_info("peername")
        if not isinstance(peer, tuple) or len(peer) < 2:
            return "", 0
        raw_host = str(peer[0])
        source_port = int(peer[1])
        try:
            parsed = ipaddress.IPv6Address(raw_host.split("%", 1)[0])
            if parsed.is_link_local and "%" not in raw_host and len(peer) >= 4 and peer[3]:
                raw_host = f"{raw_host}%{peer[3]}"
        except ValueError:
            pass
        return raw_host, source_port

    @staticmethod
    def _hello_payload() -> dict[str, Any]:
        return {
            "type": "hello",
            "app": APP_WIRE_NAME,
            "protocol": PROTOCOL,
        }

    def _local_profile_payload(self) -> dict[str, Any]:
        profile = self.vault.profile()
        addresses: list[str] = []
        try:
            addresses = [entry.host for entry in shareable_ipv6_addresses()][:12]
        except Exception:
            log.debug("failed to enumerate announced IPv6 addresses", exc_info=True)
        return {
            "name": profile.get("display_name", "我"),
            "listen_port": self.port,
            "addresses": addresses,
        }

    async def _send_hello_and_wait(self, session: EncryptedSession) -> dict[str, Any]:
        payload = self._hello_payload()
        payload.update(self._local_profile_payload())
        await session.send_control(payload)
        async with asyncio.timeout(CONTROL_TIMEOUT):
            reply = await session.receive_control()
        if reply.get("type") != "hello_ack":
            raise HandshakeError("对端没有返回有效的好友验证结果")
        return reply

    @staticmethod
    def _advertised_peer_port(reply: dict[str, Any]) -> int:
        try:
            port = int(reply.get("listen_port", 0))
            if 1 <= port <= 65535:
                return port
        except (TypeError, ValueError):
            pass
        return 0

    async def _open_peer_connection(self, contact: dict[str, Any]) -> tuple[EncryptedSession, dict[str, Any]]:
        """Connect to the first working saved IPv6 endpoint and authenticate it."""
        addresses = list(contact.get("addresses") or [])
        if not addresses:
            raise PeerUnreachable("该好友还没有保存任何 IPv6 地址")
        recent = [a for a in addresses if float(a.get("last_success", 0.0) or 0.0) > 0]
        recent.sort(key=lambda item: float(item.get("last_success", 0.0) or 0.0), reverse=True)
        older = [a for a in addresses if a not in recent]
        ordered = recent + older
        expected_peer_id = str(contact.get("peer_id") or "").lower() or None
        errors: list[str] = []
        identity_conflict = False
        self_skipped = 0
        for index, address in enumerate(ordered, start=1):
            host = str(address.get("host", ""))
            try:
                port = int(address.get("port", self.port))
                if not 1 <= port <= 65535:
                    continue
                session = await self._connect(host, port)
            except (TimeoutError, OSError, HandshakeError, ValueError) as exc:
                errors.append(f"{format_endpoint(host, port) if host else host}: {exc}")
                continue
            try:
                if session.peer_id == self.identity.peer_id:
                    # A card or a hand-edited contact may still point at
                    # ::1. Never display this as a peer identity change.
                    await self._close_session(session)
                    self_skipped += 1
                    errors.append(
                        f"{format_endpoint(host, port)}: 该地址指向本机自身，已跳过"
                    )
                    continue
                if expected_peer_id and session.peer_id != expected_peer_id:

                    await self._close_session(session)
                    identity_conflict = True
                    self._emit(
                        "security_alert",
                        contact_id=contact.get("id", ""),
                        expected=expected_peer_id,
                        actual=session.peer_id,
                        message="对方身份指纹与此前固定的指纹不一致，已阻止本次连接。如确认对方重装或更换了数据目录，请核对新指纹后重新添加联系人。",
                    )
                    continue
                try:
                    async with asyncio.timeout(CONTROL_TIMEOUT):
                        reply = await self._send_hello_and_wait(session)
                except Exception:
                    await self._close_session(session)
                    raise
                accepted = bool(reply.get("accepted"))
                learned_peer = session.peer_id
                current_contact = self.vault.get_contact(contact["id"])
                if current_contact is None:
                    await self._close_session(session)
                    raise PeerUnreachable("好友已被删除")
                if not current_contact.get("peer_id"):
                    current_contact = self.vault.bind_peer_identity(contact["id"], learned_peer)
                    self._emit_contact(current_contact, force=True)
                else:
                    expected = current_contact.get("peer_id")
                    if expected != learned_peer:
                        await self._close_session(session)
                        identity_conflict = True
                        self._emit(
                            "security_alert",
                            contact_id=contact.get("id", ""),
                            expected=expected,
                            actual=learned_peer,
                            message="对方身份指纹与此前固定的指纹不一致，已阻止本次连接。如确认对方重装或更换了数据目录，请核对新指纹后重新添加联系人。",
                        )
                        continue
                if not accepted:
                    reason = str(reply.get("reason", ""))
                    await self._close_session(session)
                    if reason == "contact_request":
                        raise PeerApprovalRequired(learned_peer)
                    if reason == "blocked":
                        raise PeerUnreachable("对方已将你加入黑名单")
                    if reason == "identity_mismatch":
                        raise PeerUnreachable("对方无法验证你的身份")
                    raise PeerUnreachable(f"对方拒绝连接：{reason or '未知原因'}")
                try:
                    updated = self.vault.add_or_touch_address(
                        contact["id"],
                        host,
                        port,
                        source="manual",
                        success=True,
                    )
                    advertised = self._advertised_peer_port(reply)
                    if advertised and (advertised != port):
                        updated = self.vault.add_or_touch_address(
                            contact["id"],
                            host,
                            advertised,
                            source="auto",
                            success=True,
                        )
                    announced_hosts = _valid_announced_hosts(reply.get("addresses"))
                    updated = self._add_announced_addresses(
                        updated,
                        announced_hosts,
                        advertised or port,
                    )
                    self._emit_contact(updated, force=True)
                except (ValueError, OSError) as exc:
                    log.debug("address book update failed: %s", exc)
                return session, reply
            except PeerApprovalRequired:
                raise
            except PeerUnreachable:
                raise
            except Exception as exc:
                await self._close_session(session)
                errors.append(str(exc))
                continue
        if identity_conflict:
            raise PeerUnreachable("对方身份指纹发生变化，连接已被阻止")
        attempts = len(ordered)
        if self_skipped == attempts:
            raise PeerUnreachable(
                "保存的地址都指向本机自身（例如回环地址 ::1），已全部跳过。"
                "请让对方重新发送一份可在网络中访问的 IPv6 名片。",
                attempts,
            )
        detail = errors[-1] if errors else "没有可用地址"
        if self_skipped:
            detail += f"；已跳过 {self_skipped} 个指向本机的地址"
        raise PeerUnreachable(f"已尝试 {attempts} 个 IPv6 地址，仍无法建立加密连接（{detail}）", attempts)

    def get_network_snapshot(self) -> dict[str, Any]:
        return {
            "listening": self._started and self._server is not None,
            "port": self.port,
            "host": self.bind_host,
        }

    # -------------------------------------------------------- inbound sessions

    async def _handle_incoming_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        session: EncryptedSession | None = None
        current = asyncio.current_task()
        if current is not None:
            self._tasks.add(current)
        try:
            async with asyncio.timeout(HANDSHAKE_TIMEOUT + 2):
                session = await server_handshake(reader, writer, self.identity)
            self._sessions.add(session)
            if session.peer_id == self.identity.peer_id:
                # Same-device loopback connection: do not create a pending
                # request from ourselves. The initiator will skip it too.
                log.debug("ignored self-connection")
                return
            async with asyncio.timeout(CONTROL_TIMEOUT):
                hello = await session.receive_control()
            if hello.get("type") != "hello":
                raise HandshakeError("连接未发送有效的加密问候")
            if hello.get("protocol") != PROTOCOL:
                raise HandshakeError("协议版本不兼容")
            peer_name = _clean_peer_name(hello.get("name"))
            advertised_port = _validate_port(hello.get("listen_port"))
            source_host, _source_port = self._source_address(writer)
            if not source_host:
                raise HandshakeError("无法识别来源 IPv6 地址")
            announced_hosts = _valid_announced_hosts(hello.get("addresses"))

            contact, accepted, reason, request = self._match_incoming_peer(
                session.peer_id,
                source_host,
                advertised_port,
                peer_name,
                announced_hosts,
            )
            ack = {
                "type": "hello_ack",
                "accepted": accepted,
                **self._local_profile_payload(),
            }
            if not accepted:
                ack["reason"] = reason
            await session.send_control(ack)

            if request is not None:
                pending = self.vault.list_pending_requests()
                self._emit("contact_request", request=request, pending=pending)
            if not accepted:
                if reason == "contact_request":
                    log.info("new encrypted contact request received")
                return
            if contact is None:
                raise HandshakeError("好友匹配失败")
            self._emit_contact(contact, force=True)
            await self._serve_peer_commands(session, contact)
        except TimeoutError:
            log.debug("incoming session timed out")
        except asyncio.IncompleteReadError:
            log.debug("incoming session disconnected")
        except (ConnectionError, OSError):
            log.debug("incoming connection closed")
        except (HandshakeError, V6LinkError) as exc:
            log.debug("incoming session rejected: %s", exc)
        except Exception:
            log.exception("unexpected incoming connection error")
        finally:
            if current is not None:
                self._tasks.discard(current)
            await self._close_session(session)
            with contextlib.suppress(Exception):
                writer.close()

    def _match_incoming_peer(
        self,
        peer_id: str,
        source_host: str,
        advertised_port: int,
        peer_name: str,
        announced_hosts: list[str] | None = None,
    ) -> tuple[dict[str, Any] | None, bool, str, dict[str, Any] | None]:
        announced_hosts = _valid_announced_hosts(announced_hosts)
        contact = self.vault.find_contact_by_peer_id(peer_id)
        if contact is not None:
            if contact.get("blocked"):
                return contact, False, "blocked", None
            try:
                contact = self.vault.add_or_touch_address(
                    contact["id"],
                    source_host,
                    advertised_port,
                    source="auto",
                    success=True,
                )
            except (ValueError, OSError):
                self.vault.touch_contact(contact["id"])
                contact = self.vault.get_contact(contact["id"]) or contact
            contact = self._add_announced_addresses(contact, announced_hosts, advertised_port)
            return contact, True, "", None

        host_matches = self.vault.contacts_matching_host(source_host)
        if host_matches:
            conflicting = [
                item for item in host_matches
                if item.get("peer_id") and item.get("peer_id") != peer_id
            ]
            if conflicting:
                expected = conflicting[0].get("peer_id", "")
                self._emit(
                    "security_alert",
                    contact_id=conflicting[0].get("id", ""),
                    expected=expected,
                    actual=peer_id,
                    message="来自好友地址的连接使用了不同的身份指纹，已阻止。",
                )
                return conflicting[0], False, "identity_mismatch", None

            empty_matches = [item for item in host_matches if not item.get("peer_id")]
            chosen: dict[str, Any] | None = None
            if len(empty_matches) == 1:
                chosen = empty_matches[0]
            elif empty_matches:
                port_matches = []
                for item in empty_matches:
                    for address in item.get("addresses", []):
                        if int(address.get("port", 0)) == advertised_port:
                            port_matches.append(item)
                            break
                unique = {item.get("id"): item for item in port_matches}
                if len(unique) == 1:
                    chosen = next(iter(unique.values()))
            if chosen is not None:
                try:
                    chosen = self.vault.bind_peer_identity(chosen["id"], peer_id)
                    chosen = self.vault.add_or_touch_address(
                        chosen["id"],
                        source_host,
                        advertised_port,
                        source="auto",
                        success=True,
                    )
                    chosen = self._add_announced_addresses(
                        chosen,
                        announced_hosts,
                        advertised_port,
                    )
                    return chosen, True, "", None
                except (ValueError, OSError):
                    pass
            expected = empty_matches[0].get("peer_id") if empty_matches else ""
            self._emit(
                "security_alert",
                contact_id=(empty_matches[0].get("id") if empty_matches else ""),
                expected=expected,
                actual=peer_id,
                message="无法将新身份安全地绑定到已有好友，已拒绝连接。",
            )
            return empty_matches[0] if empty_matches else None, False, "identity_mismatch", None

        try:
            request = self.vault.add_pending_request(
                peer_id,
                peer_name,
                source_host,
                advertised_port,
            )
        except ValueError:
            return None, False, "invalid_identity", None
        return None, False, "contact_request", request

    def _add_announced_addresses(
        self,
        contact: dict[str, Any],
        hosts: list[str],
        port: int,
    ) -> dict[str, Any]:
        for host in hosts:
            try:
                contact = self.vault.add_or_touch_address(
                    contact["id"],
                    host,
                    port,
                    source="auto",
                    success=False,
                )
            except (ValueError, OSError):
                log.debug("failed to learn announced address %s", host, exc_info=True)
        return contact

    async def _serve_peer_commands(
        self,
        session: EncryptedSession,
        contact: dict[str, Any],
    ) -> None:
        try:
            async with asyncio.timeout(CONTROL_TIMEOUT):
                command = await session.receive_control()
        except asyncio.IncompleteReadError:
            return
        command_type = command.get("type")
        if command_type == "text":
            await self._handle_incoming_text(session, contact, command)
        elif command_type == "file_offer":
            await self._handle_incoming_file_offer(session, contact, command)
        elif command_type == "stream_open":
            await self._handle_incoming_data_stream(session, contact, command)
        elif command_type == "friend_accept":
            await self._handle_friend_accept(session, contact, command)
        elif command_type == "ping":
            await session.send_control({"type": "pong"})
        else:
            await session.send_control({"type": "error", "error": "unsupported_command"})

    async def _handle_incoming_text(
        self,
        session: EncryptedSession,
        contact: dict[str, Any],
        command: dict[str, Any],
    ) -> None:
        text = command.get("text")
        origin_id = str(command.get("origin_id", ""))[:80]
        if not isinstance(text, str) or not text.strip():
            await session.send_control({"type": "text_ack", "ok": False, "error": "empty"})
            return
        if len(text) > 64 * 1024:
            await session.send_control({"type": "text_ack", "ok": False, "error": "too_large"})
            return
        existing = self.vault.find_incoming_by_origin(contact["id"], origin_id)
        if existing is not None:
            await session.send_control(
                {
                    "type": "text_ack",
                    "ok": True,
                    "origin_id": origin_id,
                    "duplicate": True,
                }
            )
            return
        created_at = _now()
        try:
            message = self.vault.add_text_message(
                contact["id"],
                "in",
                text,
                "received",
                origin_id=origin_id,
                created_at=created_at,
            )
        except ValueError as exc:
            await session.send_control({"type": "text_ack", "ok": False, "error": str(exc)})
            return
        await session.send_control(
            {
                "type": "text_ack",
                "ok": True,
                "origin_id": origin_id,
                "received_at": created_at,
            }
        )
        self._emit("message_new", message=message)
        self._emit_contact(contact)

    async def _handle_friend_accept(
        self,
        session: EncryptedSession,
        contact: dict[str, Any],
        command: dict[str, Any],
    ) -> None:
        try:
            await session.send_control({"type": "friend_accept_ack", "ok": True})
        except Exception:
            pass
        self.vault.touch_contact(contact["id"])
        updated = self.vault.get_contact(contact["id"]) or contact
        self._accepted_peer_ids.add(contact["id"])
        self._emit("friend_accepted", contact=updated)
        self._emit_contact(updated, force=True)
        self._spawn(self._retry_waiting_for(contact["id"]), name="auto-retry-waiting")

    def _schedule_waiting_retry(self, contact_id: str) -> None:
        if contact_id in self._accepted_peer_ids:
            self._spawn(self._retry_waiting_for(contact_id), name="deferred-retry-waiting")

    async def _retry_waiting_for(self, contact_id: str) -> None:
        """Retry messages that raced with the remote friend-accept notification."""
        for delay in (0.0, 0.35, 1.0):
            if delay:
                await asyncio.sleep(delay)
            messages = self.vault.messages_for_retry(contact_id, ("waiting",))
            if not messages:
                if delay >= 0.35:
                    return
                continue
            for message in messages:
                try:
                    await self.retry_message(message["id"])
                except Exception:
                    log.debug("auto retry failed", exc_info=True)
            return

    # ------------------------------------------------------- inbound file data

    async def _handle_incoming_file_offer(
        self,
        session: EncryptedSession,
        contact: dict[str, Any],
        command: dict[str, Any],
    ) -> None:
        file_id = ""
        channel: _FileControlChannel | None = None
        pending: _PendingFileOffer | None = None
        transfer: _InboundTransfer | None = None
        existing: dict[str, Any] | None = None
        writer: AttachmentWriter | None = None
        rejected = False

        async def reject(reason: str) -> None:
            nonlocal rejected
            if rejected:
                return
            rejected = True
            with contextlib.suppress(Exception):
                await session.send_control(
                    {"type": "file_reject", "file_id": file_id, "error": reason}
                )

        try:
            file_id = _validate_file_id(command.get("file_id"))
            if file_id in self._active_inbound or file_id in self._pending_offers:
                await reject("duplicate_transfer")
                return
            if len(self._active_inbound) + len(self._pending_offers) >= MAX_ACTIVE_INBOUND:
                await reject("too_many_transfers")
                return
            raw_name = str(command.get("name", "file"))
            size = int(command.get("size", 0))
            if not 0 <= size <= MAX_FILE_BYTES:
                raise TransferError("文件大小超出允许范围")
            chunk_size = _validate_chunk_size(command.get("chunk_size", DEFAULT_CHUNK))
            try:
                expected_streams = max(1, min(MAX_STREAMS, int(command.get("workers", 1))))
            except (TypeError, ValueError):
                expected_streams = 1
            fingerprint = str(command.get("fingerprint", "")).strip().lower()
            if len(fingerprint) != 64 or any(ch not in "0123456789abcdef" for ch in fingerprint):
                raise TransferError("文件完整性指纹无效")
            display_name = sanitize_filename(raw_name)

            existing = self.vault.find_incoming_by_origin(contact["id"], file_id)
            if existing is not None:
                status = str(existing.get("status", ""))
                if status == "ready":
                    await reject("already_received")
                    return
                if status == "canceled":
                    await reject("transfer_canceled")
                    return
                old_meta = existing.get("file") or {}
                old_fingerprint = str(old_meta.get("fingerprint", "") or "")
                if old_fingerprint and old_fingerprint != fingerprint:
                    await reject("fingerprint_mismatch")
                    return
                if int(old_meta.get("size", size) or 0) != size:
                    await reject("size_mismatch")
                    return
                if int(old_meta.get("chunk_size", chunk_size) or 0) != chunk_size:
                    await reject("chunk_size_mismatch")
                    return

            channel = _FileControlChannel(session, f"in-{file_id[:8]}")
            received: set[int] = set()
            received_bytes = 0
            if existing is not None:
                old_meta = existing.get("file") or {}
                if old_meta.get("enc_path"):
                    try:
                        writer = await asyncio.to_thread(
                            AttachmentWriter.open_existing,
                            self.vault.data_dir,
                            old_meta,
                        )
                        received = writer.received_indices()
                        received_bytes = writer.received_bytes
                    except Exception as exc:
                        log.warning("partial container cannot be reopened: %s", exc)
                        writer = None
                if channel.cancel_event.is_set():
                    if writer is not None:
                        await asyncio.to_thread(writer.abort)
                    await self._discard_partial_message(existing, "对方取消了文件传输")
                    return
                if channel.closed.is_set():
                    if writer is not None:
                        await asyncio.to_thread(writer.suspend)
                    return

            required = max(0, size - received_bytes) + DISK_RESERVE
            usage = await asyncio.to_thread(shutil.disk_usage, self.vault.data_dir)
            if required > max(0, int(usage.free)):
                await reject("disk_space")
                if writer is not None:
                    await asyncio.to_thread(writer.suspend)
                return

            auto_resume = existing is not None and writer is not None
            if auto_resume:
                accepted = True
                reason = "resume"
                self._emit(
                    "file_resume_started",
                    contact_id=contact["id"],
                    file_id=file_id,
                    message_id=existing["id"],
                    name=display_name,
                    received_bytes=received_bytes,
                    size=size,
                )
            else:
                pending = _PendingFileOffer(
                    file_id=file_id,
                    contact_id=contact["id"],
                    contact_name=str(contact.get("name", "好友")),
                    peer_id=session.peer_id,
                    name=display_name,
                    size=size,
                    chunk_size=chunk_size,
                    expected_streams=expected_streams,
                    fingerprint=fingerprint,
                    received_bytes=received_bytes,
                    have_bitmap="",
                    writer=writer,
                    message=existing,
                    channel=channel,
                )
                self._pending_offers[file_id] = pending
                self._emit(
                    "file_offer_received",
                    offer={
                        "file_id": file_id,
                        "contact_id": contact["id"],
                        "contact_name": pending.contact_name,
                        "name": display_name,
                        "size": size,
                        "received_bytes": received_bytes,
                        "expires_in": FILE_CONSENT_TIMEOUT,
                    },
                )
                accepted, reason = await self._wait_offer_decision(pending)
                self._pending_offers.pop(file_id, None)
                self._emit(
                    "file_offer_closed",
                    file_id=file_id,
                    contact_id=contact["id"],
                    reason=reason,
                )
                if not accepted:
                    if channel.cancel_event.is_set():
                        if writer is not None:
                            await asyncio.to_thread(writer.abort)
                        if existing is not None:
                            await self._discard_partial_message(existing, "对方取消了文件传输")
                        return
                    if channel.closed.is_set():
                        if writer is not None:
                            await asyncio.to_thread(writer.suspend)
                        return
                    await reject(reason or "declined")
                    if writer is not None:
                        await asyncio.to_thread(writer.suspend)
                    return

            if channel.cancel_event.is_set():
                if writer is not None:
                    await asyncio.to_thread(writer.abort)
                if existing is not None:
                    await self._discard_partial_message(existing, "对方取消了文件传输")
                return
            if channel.closed.is_set():
                if writer is not None:
                    await asyncio.to_thread(writer.suspend)
                return

            if writer is None:
                try:
                    writer = await asyncio.to_thread(
                        AttachmentWriter.create,
                        self.vault.data_dir,
                        size,
                        chunk_size,
                    )
                except OSError as exc:
                    await reject("create_failed")
                    raise TransferError(f"无法创建本地加密文件：{exc}") from exc
                received = set()
                received_bytes = 0
            received = writer.received_indices()
            received_bytes = writer.received_bytes
            file_meta = writer.metadata(display_name)
            file_meta.update(
                {
                    "fingerprint": fingerprint,
                    "transfer_id": file_id,
                    "received_bytes": int(received_bytes),
                }
            )
            if existing is not None:
                message = self.vault.update_message(
                    existing["id"],
                    status="receiving",
                    error="",
                    file_update=file_meta,
                )
                self._emit("message_updated", message=message)
            else:
                message = self.vault.add_file_message(
                    contact["id"],
                    "in",
                    file_meta,
                    "receiving",
                    origin_id=file_id,
                )
                self._emit("message_new", message=message)
            progress = ProgressEmitter(
                self._event_sink,
                contact["id"],
                message["id"],
                "in",
                size,
            )
            transfer = _InboundTransfer(
                file_id=file_id,
                peer_id=session.peer_id,
                contact_id=contact["id"],
                message_id=message["id"],
                writer=writer,
                size=size,
                chunk_size=chunk_size,
                expected_streams=expected_streams,
                progress=progress,
            )
            transfer.control = session
            transfer.channel = channel
            self._active_inbound[file_id] = transfer
            progress.update(received_bytes, force=True)
            have_bitmap = base64.b64encode(writer.received_bitmap()).decode("ascii")
            await session.send_control(
                {
                    "type": "file_accept",
                    "file_id": file_id,
                    "chunk_size": chunk_size,
                    "max_streams": MAX_STREAMS,
                    "have": have_bitmap,
                    "received_bytes": received_bytes,
                }
            )
            self._emit_contact(contact)
            log.info(
                "receiving encrypted file %s (%s, %d chunks, %d already present)",
                file_id,
                human_size(size),
                transfer.expected_chunks,
                len(received),
            )

            watchdog = asyncio.create_task(
                self._watch_inbound_transfer(transfer, channel),
                name=f"file-watchdog-{file_id[:8]}",
            )
            try:
                while True:
                    if transfer.error:
                        raise TransferError(transfer.error)
                    inbound_command = await channel.receive()
                    command_type = inbound_command.get("type")
                    if command_type == "file_done":
                        if str(inbound_command.get("file_id", "")) != file_id:
                            raise TransferError("文件完成标识不匹配")
                        transfer.sender_finished = True
                        break
                    if command_type == "file_cancel":
                        transfer.canceled = True
                        raise TransferError(str(inbound_command.get("error") or "对方取消了文件传输"))
                    if command_type == "ping":
                        await session.send_control({"type": "pong"})
                        continue
                    if command_type == "error":
                        raise TransferError(str(inbound_command.get("error", "发送方报告错误")))
            finally:
                watchdog.cancel()
                with contextlib.suppress(BaseException):
                    await watchdog

            if transfer.error:
                raise TransferError(transfer.error)
            deadline = asyncio.get_running_loop().time() + 45.0
            while transfer.received_chunks < transfer.expected_chunks:
                if transfer.error:
                    raise TransferError(transfer.error)
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TransferError(
                        f"文件不完整，缺少 {transfer.expected_chunks - transfer.received_chunks} 个分块"
                    )
                try:
                    await asyncio.wait_for(transfer.progress_event.wait(), timeout=min(1.0, remaining))
                except TimeoutError:
                    pass
                transfer.progress_event.clear()
            if transfer.received_bytes != size:
                raise TransferError("文件接收字节数与声明大小不一致")
            await asyncio.to_thread(transfer.writer.finish)
            transfer.finalized = True
            self._active_inbound.pop(file_id, None)
            message = self.vault.update_message(
                transfer.message_id,
                status="ready",
                error="",
                file_update={"completed_at": _now()},
            )
            transfer.progress.finish()
            self._emit("message_updated", message=message)
            await session.send_control(
                {"type": "file_result", "file_id": file_id, "ok": True}
            )
            self._spawn(
                self._auto_export_incoming(message["id"], message.get("file") or {}),
                name=f"auto-download-{message['id'][:8]}",
            )
            log.info("encrypted file received: %s", file_id)
        except (Exception, asyncio.CancelledError) as exc:
            reason = _friendly_error(exc)
            if transfer is not None and not transfer.finalized:
                if transfer.canceled:
                    await self._cancel_inbound(transfer, reason, notify_peer=False)
                else:
                    await self._pause_inbound(
                        transfer,
                        reason,
                        notify_peer=not self._stopping,
                    )
            elif pending is not None:
                self._pending_offers.pop(file_id, None)
                self._emit(
                    "file_offer_closed",
                    file_id=file_id,
                    contact_id=contact["id"],
                    reason=reason,
                )
                if not channel or (not channel.cancel_event.is_set() and not channel.closed.is_set()):
                    await reject(reason)
            elif not rejected and (not channel or (not channel.cancel_event.is_set() and not channel.closed.is_set())):
                await reject(reason)
            if isinstance(exc, asyncio.CancelledError):
                raise
        finally:
            if pending is not None:
                self._pending_offers.pop(file_id, None)
            if transfer is not None and not transfer.finalized:
                if transfer.canceled:
                    await self._cancel_inbound(transfer, "文件传输已取消", notify_peer=False)
                else:
                    await self._pause_inbound(
                        transfer,
                        "文件传输中断",
                        notify_peer=not self._stopping,
                    )
            if writer is not None:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(writer.suspend)
            if channel is not None:
                await channel.close()

    async def _wait_offer_decision(self, pending: _PendingFileOffer) -> tuple[bool, str]:
        channel = pending.channel
        if channel is None:
            return False, "disconnected"
        waits = [
            asyncio.create_task(pending.decision_event.wait(), name=f"consent-{pending.file_id[:8]}"),
            asyncio.create_task(channel.cancel_event.wait(), name=f"consent-cancel-{pending.file_id[:8]}"),
            asyncio.create_task(channel.closed.wait(), name=f"consent-close-{pending.file_id[:8]}"),
        ]
        try:
            done, _pending = await asyncio.wait(
                waits,
                timeout=FILE_CONSENT_TIMEOUT,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if channel.cancel_event.is_set():
                return False, "sender_canceled"
            if channel.closed.is_set():
                return False, "disconnected"
            if pending.decision_event.is_set():
                if pending.accepted:
                    return True, "accepted"
                return False, pending.reason or "declined"
            return False, "timeout"
        finally:
            for task in waits:
                if not task.done():
                    task.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.gather(*waits, return_exceptions=True)

    async def respond_file_offer(self, file_id: str, accept: bool) -> None:
        pending = self._pending_offers.get(str(file_id))
        if pending is None:
            raise ValueError("文件接收请求已结束或已过期")
        if pending.accepted is not None:
            return
        pending.accepted = bool(accept)
        pending.reason = "accepted" if accept else "declined"
        pending.decision_event.set()

    async def _watch_inbound_transfer(
        self,
        transfer: _InboundTransfer,
        channel: _FileControlChannel,
    ) -> None:
        """Pause a stalled transfer while preserving its partial container."""
        try:
            while not transfer.finalized and not transfer.sender_finished:
                await asyncio.sleep(3.0)
                if transfer.error:
                    break
                if (
                    transfer.received_chunks < transfer.expected_chunks
                    and _monotonic() - transfer.last_activity > 180.0
                ):
                    transfer.fail("文件传输长时间没有数据，已暂停")
                    break
            if transfer.error:
                await channel.close()
        except asyncio.CancelledError:
            return
        except Exception:
            log.debug("inbound transfer watchdog stopped", exc_info=True)

    async def _pause_inbound(
        self,
        transfer: _InboundTransfer,
        reason: str,
        *,
        notify_peer: bool = True,
    ) -> None:
        if transfer.finalized:
            return
        transfer.finalized = True
        transfer.fail(reason)
        self._active_inbound.pop(transfer.file_id, None)
        channel = transfer.channel
        if notify_peer and channel is not None and not channel.closed.is_set():
            with contextlib.suppress(Exception):
                async with asyncio.timeout(2.0):
                    await channel.session.send_control(
                        {
                            "type": "file_result",
                            "file_id": transfer.file_id,
                            "ok": False,
                            "error": reason,
                            "resumable": True,
                        }
                    )
        for data_session in list(transfer.data_sessions):
            with contextlib.suppress(BaseException):
                await self._close_session(data_session)
        transfer.data_sessions.clear()
        try:
            await asyncio.to_thread(transfer.writer.suspend)
        except Exception:
            log.debug("failed to suspend encrypted attachment", exc_info=True)
        transfer.progress.update(transfer.received_bytes, force=True)
        display_reason = reason
        if "暂停" not in display_reason and "继续" not in display_reason:
            display_reason = f"{display_reason}，传输已暂停，可继续"
        try:
            message = self.vault.update_message(
                transfer.message_id,
                status="paused",
                error=display_reason,
                file_update={"paused_at": _now()},
            )
            self._emit("message_updated", message=message)
        except Exception:
            log.debug("failed to mark inbound file paused", exc_info=True)
        if channel is not None:
            await channel.close()

    async def _cancel_inbound(
        self,
        transfer: _InboundTransfer,
        reason: str,
        *,
        notify_peer: bool = True,
    ) -> None:
        if transfer.finalized:
            return
        transfer.finalized = True
        transfer.canceled = True
        transfer.fail(reason)
        self._active_inbound.pop(transfer.file_id, None)
        channel = transfer.channel
        if notify_peer and channel is not None and not channel.closed.is_set():
            with contextlib.suppress(Exception):
                async with asyncio.timeout(2.0):
                    await channel.session.send_control(
                        {
                            "type": "file_cancel",
                            "file_id": transfer.file_id,
                            "error": "已取消接收，文件不会保留",
                        }
                    )
        for data_session in list(transfer.data_sessions):
            with contextlib.suppress(BaseException):
                await self._close_session(data_session)
        transfer.data_sessions.clear()
        try:
            await asyncio.to_thread(transfer.writer.abort)
        except Exception:
            log.debug("failed to delete canceled attachment", exc_info=True)
        transfer.progress.update(transfer.received_bytes, force=True)
        try:
            message = self.vault.update_message(
                transfer.message_id,
                status="canceled",
                error=reason,
                file_update={"canceled_at": _now()},
            )
            self._emit("message_updated", message=message)
        except Exception:
            log.debug("failed to mark inbound file canceled", exc_info=True)
        if channel is not None:
            await channel.close()

    async def _discard_partial_message(self, message: dict[str, Any], reason: str) -> None:
        file_meta = message.get("file") or {}
        relative = file_meta.get("enc_path")
        if relative:
            with contextlib.suppress(Exception):
                path = self.vault.attachment_path(relative)
                await asyncio.to_thread(path.unlink, missing_ok=True)
        with contextlib.suppress(Exception):
            updated = self.vault.update_message(
                message["id"],
                status="canceled",
                error=reason,
                file_update={"canceled_at": _now()},
            )
            self._emit("message_updated", message=updated)

    async def _handle_incoming_data_stream(
        self,
        session: EncryptedSession,
        contact: dict[str, Any],
        command: dict[str, Any],
    ) -> None:
        transfer: _InboundTransfer | None = None
        try:
            file_id = _validate_file_id(command.get("file_id"))
            transfer = self._active_inbound.get(file_id)
            if (
                transfer is None
                or transfer.peer_id != session.peer_id
                or transfer.contact_id != contact.get("id")
                or transfer.finalized
                or transfer.error
            ):
                await session.send_control(
                    {"type": "stream_reject", "file_id": file_id, "error": "unknown_transfer"}
                )
                return
            if transfer.data_streams >= max(2, transfer.expected_streams + 2):
                await session.send_control(
                    {"type": "stream_reject", "file_id": file_id, "error": "too_many_streams"}
                )
                return
            transfer.data_streams += 1
            transfer.data_sessions.add(session)
            transfer.max_streams_seen = max(transfer.max_streams_seen, transfer.data_streams)
            await session.send_control({"type": "stream_ready", "file_id": file_id})
            while True:
                if transfer.error:
                    raise TransferError(transfer.error)
                async with asyncio.timeout(300.0):
                    payload_kind, body = await session.receive_payload()
                if payload_kind == 2:
                    if len(body) < 9:
                        raise TransferError("文件数据帧无效")
                    (offset,) = struct.unpack(">Q", body[:8])
                    data = body[8:]
                    if offset % transfer.chunk_size != 0:
                        raise TransferError("文件偏移未按分块对齐")
                    index = offset // transfer.chunk_size
                    if not 0 <= index < transfer.expected_chunks:
                        raise TransferError("文件偏移超出范围")
                    expected_len = min(
                        transfer.chunk_size,
                        transfer.size - index * transfer.chunk_size,
                    )
                    if len(data) != expected_len:
                        raise TransferError("文件块长度不符合声明")
                    await asyncio.to_thread(transfer.writer.write_chunk, index, data)
                    transfer.mark_activity()
                    transfer.progress.update(transfer.received_bytes)
                    continue
                if payload_kind == 1:
                    control = _parse_control_payload(body)
                    ctype = control.get("type")
                    if ctype == "stream_end":
                        try:
                            await session.send_control(
                                {"type": "stream_done_ack", "file_id": file_id}
                            )
                        except Exception:
                            pass
                        return
                    if ctype == "ping":
                        await session.send_control({"type": "pong"})
                        continue
                    raise TransferError(f"文件数据通道收到意外控制帧：{ctype}")
                raise TransferError("文件数据通道收到未知帧")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            reason = _friendly_error(exc)
            if transfer is not None:
                transfer.fail(reason)
            log.debug("inbound data stream failed: %s", exc)
        finally:
            if transfer is not None:
                transfer.data_sessions.discard(session)
                transfer.data_streams = max(0, transfer.data_streams - 1)

    # --------------------------------------------------------- outgoing actions

    async def send_text(self, contact_id: str, text: str) -> str:
        contact = self._require_contact(contact_id)
        if contact.get("blocked"):
            raise ValueError("该好友已被拉黑")
        text = str(text or "")
        if not text.strip():
            raise ValueError("消息不能为空")
        if len(text) > 64 * 1024:
            raise ValueError("消息过长")
        message = self.vault.add_text_message(contact_id, "out", text, "sending")
        self._emit("message_new", message=message)
        self._spawn(self._deliver_text(message), name=f"text-{message['id'][:8]}")
        return message["id"]

    async def _deliver_text(self, message: dict[str, Any]) -> None:
        session: EncryptedSession | None = None
        try:
            contact = self._require_contact(message["contact_id"])
            session, _reply = await self._open_peer_connection(contact)
            await session.send_control(
                {
                    "type": "text",
                    "text": message["text"],
                    "origin_id": message["id"],
                    "created_at": message["created_at"],
                }
            )
            async with asyncio.timeout(ACK_TIMEOUT):
                ack = await session.receive_control()
            if ack.get("type") != "text_ack":
                raise HandshakeError("消息未被对方确认")
            if not ack.get("ok"):
                raise TransferError(f"对方拒收消息：{ack.get('error', '未知原因')}")
            updated = self.vault.update_message(
                message["id"],
                status="sent",
                error="",
                file_update=None,
            )
            self._emit("message_updated", message=updated)
        except PeerApprovalRequired:
            try:
                updated = self.vault.update_message(
                    message["id"],
                    status="waiting",
                    error="好友请求已发送，等待对方接受",
                )
                self._emit("message_updated", message=updated)
                self._schedule_waiting_retry(contact["id"])
                self._emit(
                    "toast",
                    message="对方还没有添加你，已发送好友请求。接受后可自动重试。",
                    level="info",
                )
            except Exception:
                log.debug("failed to mark message waiting", exc_info=True)
        except Exception as exc:
            reason = _friendly_error(exc)
            try:
                updated = self.vault.update_message(
                    message["id"],
                    status="failed",
                    error=reason,
                )
                self._emit("message_updated", message=updated)
            except Exception:
                log.debug("failed to mark message failed", exc_info=True)
        finally:
            await self._close_session(session)

    async def send_file(self, contact_id: str, file_path: str | Path) -> str:
        contact = self._require_contact(contact_id)
        if contact.get("blocked"):
            raise ValueError("该好友已被拉黑")
        source = Path(file_path).expanduser()
        if not source.exists() or not source.is_file():
            raise ValueError("文件不存在或不是普通文件")
        try:
            size = source.stat().st_size
        except OSError as exc:
            raise ValueError(f"无法读取文件：{exc}") from exc
        if size > MAX_FILE_BYTES:
            raise ValueError("文件过大")
        file_meta = {
            "name": source.name,
            "size": int(size),
            "chunk_size": DEFAULT_CHUNK,
            "source_path": str(source.resolve()),
            "storage": "external",
        }
        message = self.vault.add_file_message(contact_id, "out", file_meta, "sending")
        self._emit("message_new", message=message)
        record = _OutboundFile(message_id=message["id"])
        self._outbound_files[message["id"]] = record
        try:
            task = self._spawn(self._deliver_file(message, record), name=f"file-{message['id'][:8]}")
        except Exception:
            self._outbound_files.pop(message["id"], None)
            raise
        record.task = task
        return message["id"]

    async def _deliver_file(self, message: dict[str, Any], record: _OutboundFile) -> None:
        control: EncryptedSession | None = None
        channel: _FileControlChannel | None = None
        worker_tasks: list[asyncio.Task] = []
        message_id = message["id"]
        try:
            contact = self._require_contact(message["contact_id"])
            lock = self._file_locks.setdefault(contact["id"], asyncio.Lock())
            async with lock:
                file_meta = message.get("file") or {}
                source = Path(str(file_meta.get("source_path", "")))
                if not source.is_file():
                    raise _PermanentFileError("源文件已不存在，无法继续传输")
                size = source.stat().st_size
                if size > MAX_FILE_BYTES:
                    raise _PermanentFileError("文件过大")
                name = str(file_meta.get("name") or source.name)
                chunk_size = DEFAULT_CHUNK
                total_chunks = (size + chunk_size - 1) // chunk_size
                stored_id = str(file_meta.get("transfer_id") or "")
                try:
                    file_id = _validate_file_id(stored_id) if stored_id else ""
                except TransferError:
                    file_id = ""
                if not file_id:
                    file_id = uuid.uuid4().hex
                record.file_id = file_id
                stored_fingerprint = str(file_meta.get("fingerprint") or "")
                fingerprint = await asyncio.to_thread(_file_fingerprint, source)
                if stored_fingerprint and stored_fingerprint != fingerprint:
                    raise _PermanentFileError(
                        "源文件内容已变化，无法续传之前的文件；请重新发送该文件"
                    )
                settings = self.vault.settings()
                try:
                    configured_workers = int(settings.get("parallel_streams", 4))
                except (TypeError, ValueError):
                    configured_workers = 4
                configured_workers = max(1, min(MAX_STREAMS, configured_workers))
                updated = self.vault.update_message(
                    message_id,
                    status="sending",
                    error="",
                    file_update={
                        "size": int(size),
                        "chunk_size": chunk_size,
                        "transfer_id": file_id,
                        "fingerprint": fingerprint,
                        "source_path": str(source.resolve()),
                    },
                )
                self._emit("message_updated", message=updated)
                progress = ProgressEmitter(
                    self._event_sink,
                    contact["id"],
                    message_id,
                    "out",
                    size,
                )

                control, _reply = await self._open_peer_connection(contact)
                record.control = control
                channel = _FileControlChannel(control, f"out-{file_id[:8]}")
                record.channel = channel
                updated = self.vault.update_message(
                    message_id,
                    status="awaiting_consent",
                    error="",
                    file_update={
                        "size": int(size),
                        "chunk_size": chunk_size,
                        "transfer_id": file_id,
                        "fingerprint": fingerprint,
                    },
                )
                self._emit("message_updated", message=updated)
                await control.send_control(
                    {
                        "type": "file_offer",
                        "file_id": file_id,
                        "name": name,
                        "size": int(size),
                        "chunk_size": chunk_size,
                        "workers": max(1, min(MAX_STREAMS, configured_workers)),
                        "origin_id": message_id,
                        "fingerprint": fingerprint,
                    }
                )
                reply = await channel.receive(timeout=FILE_OFFER_TIMEOUT)
                reply_type = reply.get("type")
                if reply_type == "file_reject":
                    error = str(reply.get("error", "未知原因"))
                    if error == "already_received":
                        updated = self.vault.update_message(
                            message_id,
                            status="sent",
                            error="",
                            file_update={"completed_at": _now()},
                        )
                        progress.finish()
                        self._emit("message_updated", message=updated)
                        return
                    if error == "transfer_canceled":
                        updated = self.vault.update_message(
                            message_id,
                            status="canceled",
                            error="对方已取消过该文件传输，文件不会保留",
                        )
                        self._emit("message_updated", message=updated)
                        return
                    reason = {
                        "declined": "对方拒绝接收文件",
                        "timeout": "对方未确认接收文件",
                        "fingerprint_mismatch": "对方保存的续传文件指纹不一致",
                        "size_mismatch": "对方保存的续传文件大小不一致",
                        "chunk_size_mismatch": "对方保存的续传分块参数不一致",
                    }.get(error, f"对方拒绝接收文件：{error}")
                    updated = self.vault.update_message(message_id, status="declined", error=reason)
                    self._emit("message_updated", message=updated)
                    return
                if reply_type == "file_cancel":
                    updated = self.vault.update_message(
                        message_id,
                        status="canceled",
                        error=str(reply.get("error") or "对方取消了文件传输"),
                    )
                    self._emit("message_updated", message=updated)
                    return
                if reply_type != "file_accept":
                    raise _PermanentFileError("对方没有返回有效的文件接收确认")
                if str(reply.get("file_id", "")) != file_id:
                    raise _PermanentFileError("文件接收确认标识不匹配")
                have = _decode_have_bitmap(reply.get("have"), total_chunks)
                received_bytes = _received_bytes_for(size, chunk_size, have)
                reported_bytes = reply.get("received_bytes")
                if reported_bytes is not None:
                    try:
                        if int(reported_bytes) != received_bytes:
                            raise _PermanentFileError("续传位图与已接收字节数不一致")
                    except (TypeError, ValueError) as exc:
                        raise _PermanentFileError("续传字节数无效") from exc
                missing = [index for index in range(total_chunks) if index not in have]
                workers = min(configured_workers, len(missing)) if missing else 0
                updated = self.vault.update_message(
                    message_id,
                    status="transferring",
                    error="",
                    file_update={
                        "size": int(size),
                        "chunk_size": chunk_size,
                        "transfer_id": file_id,
                        "fingerprint": fingerprint,
                        "workers": max(1, workers),
                        "received_bytes": int(received_bytes),
                    },
                )
                self._emit("message_updated", message=updated)
                progress.update(received_bytes, force=True)
                sent_bytes = received_bytes

                if missing:
                    missing_queue: asyncio.Queue[int] = asyncio.Queue()
                    for index in missing:
                        missing_queue.put_nowait(index)

                    async def worker(worker_index: int) -> None:
                        nonlocal sent_bytes
                        data_session: EncryptedSession | None = None
                        handle = None
                        try:
                            data_session, _ = await self._open_peer_connection(contact)
                            await data_session.send_control(
                                {"type": "stream_open", "file_id": file_id}
                            )
                            async with asyncio.timeout(ACK_TIMEOUT):
                                stream_ack = await data_session.receive_control()
                            if stream_ack.get("type") != "stream_ready":
                                raise TransferError(
                                    f"数据通道被拒绝：{stream_ack.get('error', '未知原因')}"
                                )
                            handle = await asyncio.to_thread(open, source, "rb")
                            while True:
                                if channel.cancel_event.is_set():
                                    raise TransferError(channel.cancel_reason or "对方取消了文件传输")
                                try:
                                    index = missing_queue.get_nowait()
                                except asyncio.QueueEmpty:
                                    break
                                offset = index * chunk_size
                                length = min(chunk_size, size - offset)
                                data = await asyncio.to_thread(_read_range, handle, offset, length)
                                if len(data) != length:
                                    raise _PermanentFileError("读取源文件时遇到意外的文件结尾")
                                await data_session.send_file_chunk(offset, data)
                                sent_bytes += len(data)
                                progress.update(sent_bytes)
                            await data_session.send_control({"type": "stream_end", "file_id": file_id})
                            async with asyncio.timeout(ACK_TIMEOUT):
                                end_ack = await data_session.receive_control()
                            if end_ack.get("type") != "stream_done_ack":
                                raise TransferError("文件数据通道未正常结束")
                        finally:
                            if handle is not None:
                                with contextlib.suppress(Exception):
                                    await asyncio.to_thread(handle.close)
                            await self._close_session(data_session)

                    worker_tasks = [
                        asyncio.create_task(worker(index), name=f"file-worker-{index}")
                        for index in range(workers)
                    ]

                    async def cancel_workers_when_peer_cancels() -> None:
                        await channel.cancel_event.wait()
                        for task in worker_tasks:
                            if not task.done():
                                task.cancel()

                    peer_cancel_watch = asyncio.create_task(
                        cancel_workers_when_peer_cancels(),
                        name=f"file-peer-cancel-{file_id[:8]}",
                    )
                    try:
                        done, pending = await asyncio.wait(
                            worker_tasks,
                            return_when=asyncio.FIRST_EXCEPTION,
                        )
                    finally:
                        peer_cancel_watch.cancel()
                        with contextlib.suppress(BaseException):
                            await peer_cancel_watch
                    if channel.cancel_event.is_set():
                        for task in pending:
                            task.cancel()
                        await asyncio.gather(*worker_tasks, return_exceptions=True)
                        updated = self.vault.update_message(
                            message_id,
                            status="canceled",
                            error=channel.cancel_reason or "接收方取消了文件传输，文件不会保留",
                        )
                        self._emit("message_updated", message=updated)
                        return
                    errors: list[BaseException] = []
                    for task in done:
                        if task.cancelled():
                            continue
                        exc = task.exception()
                        if exc is not None:
                            errors.append(exc)
                    if errors:
                        for task in pending:
                            task.cancel()
                        await asyncio.gather(*pending, return_exceptions=True)
                        raise errors[0]
                    if pending:
                        await asyncio.gather(*pending)

                await control.send_control({"type": "file_done", "file_id": file_id})
                result: dict[str, Any] | None = None
                async with asyncio.timeout(FILE_RESULT_TIMEOUT):
                    while True:
                        inbound = await channel.receive()
                        inbound_type = inbound.get("type")
                        if inbound_type == "file_result":
                            result = inbound
                            break
                        if inbound_type == "file_cancel":
                            updated = self.vault.update_message(
                                message_id,
                                status="canceled",
                                error=str(inbound.get("error") or "接收方取消了文件传输"),
                            )
                            self._emit("message_updated", message=updated)
                            return
                        if inbound_type == "ping":
                            await control.send_control({"type": "pong"})
                            continue
                        if inbound_type == "error":
                            raise TransferError(str(inbound.get("error", "对方报告文件错误")))
                if result is None or not result.get("ok"):
                    raise TransferError(
                        f"对方未能完整接收文件：{(result or {}).get('error', '未知原因')}"
                    )
                updated = self.vault.update_message(
                    message_id,
                    status="sent",
                    error="",
                    file_update={
                        "transfer_id": file_id,
                        "completed_at": _now(),
                    },
                )
                progress.finish()
                self._emit("message_updated", message=updated)
        except PeerApprovalRequired:
            try:
                updated = self.vault.update_message(
                    message_id,
                    status="waiting",
                    error="好友请求已发送，等待对方接受",
                )
                self._emit("message_updated", message=updated)
                self._schedule_waiting_retry(message["contact_id"])
                self._emit(
                    "toast",
                    message="对方还没有添加你，已发送好友请求。接受后可自动重试。",
                    level="info",
                )
            except Exception:
                log.debug("failed to mark file waiting", exc_info=True)
        except asyncio.CancelledError:
            try:
                current = self.vault.get_message(message_id)
                if current is not None and current.get("status") not in (
                    "canceled",
                    "paused",
                    "declined",
                    "sent",
                    "waiting",
                ):
                    updated = self.vault.update_message(
                        message_id,
                        status="paused",
                        error="传输已暂停，可继续",
                    )
                    self._emit("message_updated", message=updated)
            except Exception:
                log.debug("failed to mark file paused on cancel", exc_info=True)
            raise
        except Exception as exc:
            reason = _friendly_error(exc)
            try:
                current = self.vault.get_message(message_id)
                if current is None or current.get("status") in (
                    "canceled",
                    "paused",
                    "declined",
                    "sent",
                    "waiting",
                ):
                    return
                if channel is not None and channel.cancel_event.is_set():
                    status = "canceled"
                elif isinstance(exc, _PermanentFileError):
                    status = "failed"
                else:
                    status = "paused"
                updated = self.vault.update_message(
                    message_id,
                    status=status,
                    error=reason if status != "paused" else f"{reason}，可继续传输",
                )
                self._emit("message_updated", message=updated)
            except Exception:
                log.debug("failed to mark outgoing file paused", exc_info=True)
        finally:
            for task in worker_tasks:
                if not task.done():
                    task.cancel()
            if worker_tasks:
                with contextlib.suppress(BaseException):
                    await asyncio.gather(*worker_tasks, return_exceptions=True)
            if self._outbound_files.get(message_id) is record:
                self._outbound_files.pop(message_id, None)
            if channel is not None:
                with contextlib.suppress(BaseException):
                    await channel.close()
            elif control is not None:
                with contextlib.suppress(BaseException):
                    await self._close_session(control)

    async def retry_message(self, message_id: str) -> None:
        message = self.vault.get_message(message_id)
        if message is None:
            raise ValueError("消息不存在")
        if message.get("direction") != "out":
            raise ValueError("只能重试已发送的消息")
        existing_record = self._outbound_files.get(message_id)
        if existing_record is not None and existing_record.task is not None and not existing_record.task.done():
            raise ValueError("该消息正在发送中")
        if message.get("kind") == "text":
            if message.get("status") not in ("failed", "waiting"):
                raise ValueError("该消息当前不需要重试")
            updated = self.vault.update_message(message_id, status="sending", error="")
            self._emit("message_updated", message=updated)
            self._spawn(self._deliver_text(updated), name=f"retry-text-{message_id[:8]}")
            return
        if message.get("status") not in ("failed", "waiting", "paused", "declined"):
            raise ValueError("该文件当前不能继续传输")
        file_meta = message.get("file") or {}
        source = Path(str(file_meta.get("source_path", "")))
        if not source.is_file():
            raise ValueError("源文件已不存在，无法继续")
        updated = self.vault.update_message(message_id, status="sending", error="")
        self._emit("message_updated", message=updated)
        record = _OutboundFile(message_id=message_id)
        self._outbound_files[message_id] = record
        try:
            task = self._spawn(
                self._deliver_file(updated, record),
                name=f"retry-file-{message_id[:8]}",
            )
        except Exception:
            self._outbound_files.pop(message_id, None)
            raise
        record.task = task

    async def cancel_transfer(self, message_id: str) -> None:
        """Explicitly cancel: the receiver must delete its partial container."""
        message = self.vault.get_message(message_id)
        if message is None:
            raise ValueError("消息不存在")
        if message.get("kind") != "file":
            raise ValueError("只有文件传输可以取消")
        if message.get("direction") == "out":
            if message.get("status") not in ("sending", "awaiting_consent", "transferring"):
                raise ValueError("该文件当前没有正在进行的传输")
            record = self._outbound_files.get(message_id)
            updated = self.vault.update_message(
                message_id,
                status="canceled",
                error="已取消传输，文件不会保留",
                file_update={"canceled_at": _now()},
            )
            self._emit("message_updated", message=updated)
            if record is not None and record.channel is not None:
                with contextlib.suppress(Exception):
                    async with asyncio.timeout(2.0):
                        await record.channel.session.send_control(
                            {
                                "type": "file_cancel",
                                "file_id": record.file_id,
                                "error": "已取消发送，文件不会保留",
                            }
                        )
            if record is not None and record.task is not None and not record.task.done():
                record.task.cancel()
            return

        if message.get("status") != "receiving":
            raise ValueError("该文件当前没有正在接收")
        transfer = next(
            (
                item
                for item in self._active_inbound.values()
                if item.message_id == message_id
            ),
            None,
        )
        if transfer is not None:
            await self._cancel_inbound(
                transfer,
                "已取消接收，文件不会保留",
                notify_peer=True,
            )
            return
        file_meta = message.get("file") or {}
        relative = file_meta.get("enc_path")
        if relative:
            with contextlib.suppress(Exception):
                path = self.vault.attachment_path(relative)
                await asyncio.to_thread(path.unlink, missing_ok=True)
        updated = self.vault.update_message(
            message_id,
            status="canceled",
            error="已取消接收，文件不会保留",
            file_update={"canceled_at": _now()},
        )
        self._emit("message_updated", message=updated)

    # ------------------------------------------------------------ contact APIs

    def _require_contact(self, contact_id: str) -> dict[str, Any]:
        contact = self.vault.get_contact(contact_id)
        if contact is None:
            raise ValueError("好友不存在或已被删除")
        return contact

    async def add_contact(
        self,
        name: str,
        peer_id: str = "",
        addresses: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        contact = self.vault.create_contact(name, peer_id=peer_id, addresses=addresses or [])
        self._emit("contact_added", contact=contact)
        return contact

    async def update_contact(self, contact_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        contact = self.vault.update_contact(contact_id, **changes)
        self._emit("contact_updated", contact=contact)
        return contact

    async def delete_contact(self, contact_id: str) -> None:
        contact = self.vault.get_contact(contact_id)
        if contact is None:
            return
        self.vault.delete_contact(contact_id)
        self._emit("contact_deleted", contact_id=contact_id)

    async def accept_pending_request(self, request_id: str) -> dict[str, Any]:
        request = self.vault.get_pending_request(request_id)
        if request is None:
            raise ValueError("好友请求已不存在")
        existing = self.vault.find_contact_by_peer_id(request["peer_id"])
        if existing is not None:
            self.vault.remove_pending_request(request_id)
            return existing
        contact = self.vault.create_contact(
            request.get("name", "新联系人"),
            peer_id=request.get("peer_id", ""),
            addresses=[
                {
                    "host": request.get("host", ""),
                    "port": request.get("port", 4455),
                    "label": "好友请求来源",
                    "source": "auto",
                }
            ],
        )
        self.vault.remove_pending_request(request_id)
        self._emit("contact_added", contact=contact)
        self._emit("pending_requests_changed", pending=self.vault.list_pending_requests())
        self._spawn(self._notify_friend_accepted(contact), name="friend-accept-notify")
        return contact

    async def decline_pending_request(self, request_id: str) -> None:
        self.vault.remove_pending_request(request_id)
        self._emit("pending_requests_changed", pending=self.vault.list_pending_requests())

    async def _notify_friend_accepted(self, contact: dict[str, Any]) -> None:
        session: EncryptedSession | None = None
        try:
            session, _reply = await self._open_peer_connection(contact)
            await session.send_control({"type": "friend_accept", "name": self.vault.profile().get("display_name", "我")})
            async with asyncio.timeout(ACK_TIMEOUT):
                ack = await session.receive_control()
            if ack.get("type") != "friend_accept_ack":
                raise HandshakeError("好友接受通知未被确认")
        except Exception as exc:
            self._emit(
                "toast",
                message=f"已添加好友，但暂时无法通知对方：{_friendly_error(exc)}",
                level="warning",
            )
        finally:
            await self._close_session(session)

    # ---------------------------------------------------------------- settings

    async def apply_settings(
        self,
        display_name: str,
        listen_port: int,
        parallel_streams: int | None = None,
        downloads_dir: str | Path | None = None,
    ) -> None:
        name = str(display_name or "").strip()
        if not name:
            raise ValueError("昵称不能为空")
        if len(name) > 40:
            raise ValueError("昵称不能超过 40 个字符")
        try:
            new_port = int(listen_port)
        except (TypeError, ValueError) as exc:
            raise ValueError("端口必须是数字") from exc
        if not 1 <= new_port <= 65535:
            raise ValueError("端口必须在 1–65535 之间")
        target_downloads: Path | None = None
        stored_downloads: str | None = None
        if downloads_dir is not None:
            raw_downloads = str(downloads_dir).strip()
            target_downloads = (
                Path(raw_downloads).expanduser()
                if raw_downloads
                else Path.home() / "Downloads"
            )
            try:
                target_downloads.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise ValueError(f"无法使用下载目录：{exc}") from exc
            if not target_downloads.is_dir():
                raise ValueError("下载目录不是文件夹")
            stored_downloads = str(target_downloads.resolve()) if raw_downloads else ""
        old_port = self.port
        listener_down = self._server is None
        if new_port != old_port or listener_down:
            server = self._server
            self._server = None
            if server is not None:
                server.close()
                with contextlib.suppress(Exception):
                    await server.wait_closed()
            try:
                await self._start_listener(new_port)
                self._started = True
            except OSError as exc:
                try:
                    await self._start_listener(old_port)
                    self.port = old_port
                except OSError:
                    self._server = None
                raise ValueError(f"无法监听新端口：{exc.strerror or exc}") from exc
        self.vault.update_profile(name, new_port, parallel_streams)
        if target_downloads is not None and stored_downloads is not None:
            self._downloads_dir = target_downloads
            self.vault.set_downloads_dir(stored_downloads)
        self._emit(
            "network",
            listening=True,
            port=self.port,
            host=self.bind_host,
        )
        self._emit("profile_updated", profile=self.vault.profile(), settings=self.vault.settings())

    # ------------------------------------------------------------- attachments

    @staticmethod
    def _unique_download_path(folder: Path, name: str) -> Path:
        safe_name = sanitize_filename(name or "download.bin", fallback="download.bin")
        candidate = folder / safe_name
        if not candidate.exists():
            return candidate
        stem = Path(safe_name).stem or "download"
        suffix = Path(safe_name).suffix
        for index in range(1, 10000):
            candidate = folder / f"{stem} ({index}){suffix}"
            if not candidate.exists():
                return candidate
        raise TransferError("下载目录中同名文件过多，无法生成新文件名")

    async def _auto_export_incoming(self, message_id: str, file_meta: dict[str, Any]) -> None:
        from .filestore import decrypt_to_file

        name = str(file_meta.get("name", "download.bin"))
        try:
            async with self._download_lock:
                folder = self._downloads_dir
                await asyncio.to_thread(folder.mkdir, parents=True, exist_ok=True)
                target = await asyncio.to_thread(self._unique_download_path, folder, name)
                path = await asyncio.to_thread(
                    decrypt_to_file,
                    self.vault.data_dir,
                    file_meta,
                    target,
                )
                updated = self.vault.update_message(
                    message_id,
                    file_update={
                        "export_path": str(path),
                        "auto_downloaded": True,
                        "exported_at": _now(),
                    },
                )
                self._emit("message_updated", message=updated)
                self._emit(
                    "file_auto_saved",
                    message_id=message_id,
                    contact_id=updated.get("contact_id", ""),
                    name=name,
                    path=str(path),
                )
                log.info("incoming file auto-saved to downloads: %s", path.name)
        except Exception as exc:
            reason = _friendly_error(exc)
            log.warning("auto-save incoming file failed: %s", reason)
            self._emit(
                "toast",
                message=f"文件已接收，但自动保存到下载目录失败：{reason}。可在文件卡片上选择「另存为」。",
                level="warning",
            )

    async def export_attachment(self, message_id: str, destination: str | Path) -> Path:
        from .filestore import decrypt_to_file

        message = self.vault.get_message(message_id)
        if message is None or message.get("kind") != "file":
            raise ValueError("找不到该文件消息")
        file_meta = message.get("file") or {}
        if message.get("direction") != "in" or file_meta.get("storage") != "encrypted":
            raise ValueError("该文件没有加密存储副本")
        target = Path(destination).expanduser()
        path = await asyncio.to_thread(
            decrypt_to_file,
            self.vault.data_dir,
            file_meta,
            target,
        )
        try:
            updated = self.vault.update_message(
                message_id,
                file_update={
                    "export_path": str(path),
                    "exported_at": _now(),
                },
            )
            self._emit("message_updated", message=updated)
        except Exception:
            log.debug("failed to remember exported file path", exc_info=True)
        self._emit("attachment_open", message_id=message_id, path=str(path), opened=False)
        return path

    async def open_attachment(self, message_id: str) -> Path:
        from .filestore import decrypt_to_temp

        message = self.vault.get_message(message_id)
        if message is None or message.get("kind") != "file":
            raise ValueError("找不到该文件消息")
        file_meta = message.get("file") or {}
        if message.get("direction") != "in" or file_meta.get("storage") != "encrypted":
            raise ValueError("该文件没有加密存储副本")
        path = await asyncio.to_thread(
            decrypt_to_temp,
            self.vault.data_dir,
            file_meta,
        )
        self._emit("attachment_open", message_id=message_id, path=str(path), opened=True)
        return path

    # ------------------------------------------------------------------- state

    def snapshot(self) -> dict[str, Any]:
        profile = self.vault.profile()
        return {
            "profile": profile,
            "settings": self.vault.settings(),
            "peer_id": self.identity.peer_id,
            "contacts": self.vault.list_contacts(),
            "pending_requests": self.vault.list_pending_requests(),
            "network": self.get_network_snapshot(),
        }


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
    if isinstance(exc, (PeerUnreachable, PeerApprovalRequired, IdentityMismatch, HandshakeError, TransferError)):
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













