"""Outgoing file transfers and cancellation."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ...crypto import (
    EncryptedSession,
)
from ...errors import (
    PeerApprovalRequired,
    TransferError,
)
from ...filestore import DEFAULT_CHUNK
from ...vault import Vault

from ..connections import ConnectionManager
from ..contacts import ContactService
from ..events import EventBus
from ..models import (
    ACK_TIMEOUT,
    FILE_OFFER_TIMEOUT,
    FILE_RESULT_TIMEOUT,
    MAX_FILE_BYTES,
    MAX_STREAMS,
    log,
    _now,
    _validate_file_id,
    _file_fingerprint,
    _decode_have_bitmap,
    _received_bytes_for,
    _FileControlChannel,
    _OutboundFile,
    _PermanentFileError,
    ProgressEmitter,
    _read_range,
    _friendly_error,
)
from ..tasks import TaskRegistry
from .state import FileState

WaitingRetry = Callable[[str], None]


class OutboundTransferService:
    """Sends files over parallel encrypted sessions."""

    def __init__(
        self,
        *,
        vault: Vault,
        events: EventBus,
        tasks: TaskRegistry,
        contacts: ContactService,
        connections: ConnectionManager,
        state: FileState,
        schedule_waiting_retry: WaitingRetry,
    ):
        self.vault = vault
        self.events = events
        self.tasks = tasks
        self.contacts = contacts
        self.connections = connections
        self.state = state
        self.schedule_waiting_retry = schedule_waiting_retry

    async def send_file(self, contact_id: str, file_path: str | Path) -> str:
        contact = self.contacts.require_contact(contact_id)
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
        self.events.emit("message_new", message=message)
        record = _OutboundFile(message_id=message["id"])
        self.state.outbound_files[message["id"]] = record
        try:
            task = self.tasks.spawn(
                self.deliver_file(message, record),
                name=f"file-{message['id'][:8]}",
            )
        except Exception:
            self.state.outbound_files.pop(message["id"], None)
            raise
        record.task = task
        return message["id"]

    async def deliver_file(self, message: dict[str, Any], record: _OutboundFile) -> None:
        control: EncryptedSession | None = None
        channel: _FileControlChannel | None = None
        worker_tasks: list[asyncio.Task] = []
        message_id = message["id"]
        try:
            contact = self.contacts.require_contact(message["contact_id"])
            lock = self.state.file_locks.setdefault(contact["id"], asyncio.Lock())
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
                self.events.emit("message_updated", message=updated)
                progress = ProgressEmitter(
                    self.events.sink,
                    contact["id"],
                    message_id,
                    "out",
                    size,
                )

                control, _reply = await self.connections.open_peer_connection(contact)
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
                self.events.emit("message_updated", message=updated)
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
                        self.events.emit("message_updated", message=updated)
                        return
                    if error == "transfer_canceled":
                        updated = self.vault.update_message(
                            message_id,
                            status="canceled",
                            error="对方已取消过该文件传输，文件不会保留",
                        )
                        self.events.emit("message_updated", message=updated)
                        return
                    reason = {
                        "declined": "对方拒绝接收文件",
                        "timeout": "对方未确认接收文件",
                        "fingerprint_mismatch": "对方保存的续传文件指纹不一致",
                        "size_mismatch": "对方保存的续传文件大小不一致",
                        "chunk_size_mismatch": "对方保存的续传分块参数不一致",
                    }.get(error, f"对方拒绝接收文件：{error}")
                    updated = self.vault.update_message(message_id, status="declined", error=reason)
                    self.events.emit("message_updated", message=updated)
                    return
                if reply_type == "file_cancel":
                    updated = self.vault.update_message(
                        message_id,
                        status="canceled",
                        error=str(reply.get("error") or "对方取消了文件传输"),
                    )
                    self.events.emit("message_updated", message=updated)
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
                self.events.emit("message_updated", message=updated)
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
                            data_session, _ = await self.connections.open_peer_connection(contact)
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
                            await self.connections.close_session(data_session)

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
                        self.events.emit("message_updated", message=updated)
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
                            self.events.emit("message_updated", message=updated)
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
                self.events.emit("message_updated", message=updated)
        except PeerApprovalRequired:
            try:
                updated = self.vault.update_message(
                    message_id,
                    status="waiting",
                    error="好友请求已发送，等待对方接受",
                )
                self.events.emit("message_updated", message=updated)
                self.schedule_waiting_retry(message["contact_id"])
                self.events.emit(
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
                    self.events.emit("message_updated", message=updated)
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
                self.events.emit("message_updated", message=updated)
            except Exception:
                log.debug("failed to mark outgoing file paused", exc_info=True)
        finally:
            for task in worker_tasks:
                if not task.done():
                    task.cancel()
            if worker_tasks:
                with contextlib.suppress(BaseException):
                    await asyncio.gather(*worker_tasks, return_exceptions=True)
            if self.state.outbound_files.get(message_id) is record:
                self.state.outbound_files.pop(message_id, None)
            if channel is not None:
                with contextlib.suppress(BaseException):
                    await channel.close()
            elif control is not None:
                with contextlib.suppress(BaseException):
                    await self.connections.close_session(control)

    async def retry_file(self, message_id: str) -> None:
        message = self.vault.get_message(message_id)
        if message is None:
            raise ValueError("消息不存在")
        if message.get("direction") != "out":
            raise ValueError("只能重试已发送的消息")
        existing_record = self.state.outbound_files.get(message_id)
        if (
            existing_record is not None
            and existing_record.task is not None
            and not existing_record.task.done()
        ):
            raise ValueError("该消息正在发送中")
        if message.get("kind") != "file":
            raise ValueError("该消息不是文件消息")
        if message.get("status") not in ("failed", "waiting", "paused", "declined"):
            raise ValueError("该文件当前不能继续传输")
        file_meta = message.get("file") or {}
        source = Path(str(file_meta.get("source_path", "")))
        if not source.is_file():
            raise ValueError("源文件已不存在，无法继续")
        updated = self.vault.update_message(message_id, status="sending", error="")
        self.events.emit("message_updated", message=updated)
        record = _OutboundFile(message_id=message_id)
        self.state.outbound_files[message_id] = record
        try:
            task = self.tasks.spawn(
                self.deliver_file(updated, record),
                name=f"retry-file-{message_id[:8]}",
            )
        except Exception:
            self.state.outbound_files.pop(message_id, None)
            raise
        record.task = task