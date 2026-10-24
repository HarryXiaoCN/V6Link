"""Incoming file offers, data streams, pause and cancel handling."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import shutil
import struct
from collections.abc import Awaitable, Callable
from typing import Any

from ...crypto import (
    EncryptedSession,
)
from ...errors import (
    TransferError,
)
from ...filestore import DEFAULT_CHUNK, AttachmentWriter
from ...netutil import (
    human_size,
    sanitize_filename,
)
from ...vault import Vault

from ..connections import ConnectionManager
from ..contacts import ContactService
from ..events import EventBus
from ..models import (
    FILE_CONSENT_TIMEOUT,
    MAX_FILE_BYTES,
    MAX_STREAMS,
    MAX_ACTIVE_INBOUND,
    DISK_RESERVE,
    log,
    _now,
    _monotonic,
    _validate_file_id,
    _FileControlChannel,
    _PendingFileOffer,
    _validate_chunk_size,
    ProgressEmitter,
    _InboundTransfer,
    _parse_control_payload,
    _friendly_error,
)
from ..tasks import TaskRegistry
from .state import FileState

AutoExport = Callable[[str, dict[str, Any]], Awaitable[None]]


class InboundTransferService:
    """Accepts file offers, writes encrypted chunks and manages resumability."""

    def __init__(
        self,
        *,
        vault: Vault,
        events: EventBus,
        tasks: TaskRegistry,
        contacts: ContactService,
        connections: ConnectionManager,
        state: FileState,
        auto_export: AutoExport,
    ):
        self.vault = vault
        self.events = events
        self.tasks = tasks
        self.contacts = contacts
        self.connections = connections
        self.state = state
        self.auto_export = auto_export

    async def handle_offer(
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
            if file_id in self.state.active_inbound or file_id in self.state.pending_offers:
                await reject("duplicate_transfer")
                return
            if (
                len(self.state.active_inbound) + len(self.state.pending_offers)
                >= MAX_ACTIVE_INBOUND
            ):
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
                    await self.discard_partial(existing, "对方取消了文件传输")
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
                self.events.emit(
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
                self.state.pending_offers[file_id] = pending
                self.events.emit(
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
                accepted, reason = await self.wait_offer_decision(pending)
                self.state.pending_offers.pop(file_id, None)
                self.events.emit(
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
                            await self.discard_partial(existing, "对方取消了文件传输")
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
                    await self.discard_partial(existing, "对方取消了文件传输")
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
                self.events.emit("message_updated", message=message)
            else:
                message = self.vault.add_file_message(
                    contact["id"],
                    "in",
                    file_meta,
                    "receiving",
                    origin_id=file_id,
                )
                self.events.emit("message_new", message=message)
            progress = ProgressEmitter(
                self.events.sink,
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
            self.state.active_inbound[file_id] = transfer
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
            self.events.emit_contact(contact)
            log.info(
                "receiving encrypted file %s (%s, %d chunks, %d already present)",
                file_id,
                human_size(size),
                transfer.expected_chunks,
                len(received),
            )

            watchdog = asyncio.create_task(
                self.watch(transfer, channel),
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
            self.state.active_inbound.pop(file_id, None)
            message = self.vault.update_message(
                transfer.message_id,
                status="ready",
                error="",
                file_update={"completed_at": _now()},
            )
            transfer.progress.finish()
            self.events.emit("message_updated", message=message)
            await session.send_control(
                {"type": "file_result", "file_id": file_id, "ok": True}
            )
            self.tasks.spawn(
                self.auto_export(message["id"], message.get("file") or {}),
                name=f"auto-download-{message['id'][:8]}",
            )
            log.info("encrypted file received: %s", file_id)
        except (Exception, asyncio.CancelledError) as exc:
            reason = _friendly_error(exc)
            if transfer is not None and not transfer.finalized:
                if transfer.canceled:
                    await self.cancel(transfer, reason, notify_peer=False)
                else:
                    await self.pause(
                        transfer,
                        reason,
                        notify_peer=not self.state.stopping,
                    )
            elif pending is not None:
                self.state.pending_offers.pop(file_id, None)
                self.events.emit(
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
                self.state.pending_offers.pop(file_id, None)
            if transfer is not None and not transfer.finalized:
                if transfer.canceled:
                    await self.cancel(transfer, "文件传输已取消", notify_peer=False)
                else:
                    await self.pause(
                        transfer,
                        "文件传输中断",
                        notify_peer=not self.state.stopping,
                    )
            if writer is not None:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(writer.suspend)
            if channel is not None:
                await channel.close()
    async def wait_offer_decision(self, pending: _PendingFileOffer) -> tuple[bool, str]:
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

    async def respond_offer(self, file_id: str, accept: bool) -> None:
        pending = self.state.pending_offers.get(str(file_id))
        if pending is None:
            raise ValueError("文件接收请求已结束或已过期")
        if pending.accepted is not None:
            return
        pending.accepted = bool(accept)
        pending.reason = "accepted" if accept else "declined"
        pending.decision_event.set()

    async def watch(
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

    async def pause(
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
        self.state.active_inbound.pop(transfer.file_id, None)
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
                await self.connections.close_session(data_session)
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
            self.events.emit("message_updated", message=message)
        except Exception:
            log.debug("failed to mark inbound file paused", exc_info=True)
        if channel is not None:
            await channel.close()

    async def cancel(
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
        self.state.active_inbound.pop(transfer.file_id, None)
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
                await self.connections.close_session(data_session)
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
            self.events.emit("message_updated", message=message)
        except Exception:
            log.debug("failed to mark inbound file canceled", exc_info=True)
        if channel is not None:
            await channel.close()

    async def discard_partial(self, message: dict[str, Any], reason: str) -> None:
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
            self.events.emit("message_updated", message=updated)

    async def handle_data_stream(
        self,
        session: EncryptedSession,
        contact: dict[str, Any],
        command: dict[str, Any],
    ) -> None:
        transfer: _InboundTransfer | None = None
        try:
            file_id = _validate_file_id(command.get("file_id"))
            transfer = self.state.active_inbound.get(file_id)
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