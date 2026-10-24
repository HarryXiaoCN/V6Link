"""File transfer facade: inbound and outbound services plus shared export."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

from ...crypto import EncryptedSession
from ...vault import Vault

from ..connections import ConnectionManager
from ..contacts import ContactService
from ..events import EventBus
from ..models import log, _now
from ..tasks import TaskRegistry
from .exports import ExportService
from .inbound import InboundTransferService
from .outbound import OutboundTransferService, WaitingRetry
from .state import FileState


class FileTransferService:
    """Coordinates inbound and outbound file transfers.

    Each direction lives in its own service; this class owns the shared state
    and the two operations that span both directions: explicit cancellation and
    shutdown pausing.
    """

    def __init__(
        self,
        *,
        vault: Vault,
        events: EventBus,
        tasks: TaskRegistry,
        contacts: ContactService,
        connections: ConnectionManager,
        downloads_dir: str | Path,
        schedule_waiting_retry: WaitingRetry,
    ):
        self.vault = vault
        self.events = events
        self.tasks = tasks
        self.contacts = contacts
        self.connections = connections
        self.state = FileState(downloads_dir=Path(downloads_dir).expanduser())
        self.exports = ExportService(vault=vault, events=events, state=self.state)
        self.inbound = InboundTransferService(
            vault=vault,
            events=events,
            tasks=tasks,
            contacts=contacts,
            connections=connections,
            state=self.state,
            auto_export=self.exports.auto_export,
        )
        self.outbound = OutboundTransferService(
            vault=vault,
            events=events,
            tasks=tasks,
            contacts=contacts,
            connections=connections,
            state=self.state,
            schedule_waiting_retry=schedule_waiting_retry,
        )

    @property
    def downloads_dir(self) -> Path:
        return self.state.downloads_dir

    def set_downloads_dir(self, path: str | Path) -> None:
        self.state.downloads_dir = Path(path).expanduser()

    async def handle_offer(
        self,
        session: EncryptedSession,
        contact: dict[str, Any],
        command: dict[str, Any],
    ) -> None:
        await self.inbound.handle_offer(session, contact, command)

    async def handle_data_stream(
        self,
        session: EncryptedSession,
        contact: dict[str, Any],
        command: dict[str, Any],
    ) -> None:
        await self.inbound.handle_data_stream(session, contact, command)

    async def send_file(self, contact_id: str, file_path: str | Path) -> str:
        return await self.outbound.send_file(contact_id, file_path)

    async def retry_file(self, message_id: str) -> None:
        await self.outbound.retry_file(message_id)

    async def respond_file_offer(self, file_id: str, accept: bool) -> None:
        await self.inbound.respond_offer(file_id, accept)

    async def export_attachment(self, message_id: str, destination: str | Path) -> Path:
        return await self.exports.export_attachment(message_id, destination)

    async def open_attachment(self, message_id: str) -> Path:
        return await self.exports.open_attachment(message_id)

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
            record = self.state.outbound_files.get(message_id)
            updated = self.vault.update_message(
                message_id,
                status="canceled",
                error="已取消传输，文件不会保留",
                file_update={"canceled_at": _now()},
            )
            self.events.emit("message_updated", message=updated)
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
                for item in self.state.active_inbound.values()
                if item.message_id == message_id
            ),
            None,
        )
        if transfer is not None:
            await self.inbound.cancel(
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
        self.events.emit("message_updated", message=updated)

    async def pause_all(self) -> None:
        """Pause every active transfer before the engine shuts down."""
        self.state.stopping = True
        for transfer in list(self.state.active_inbound.values()):
            await self.inbound.pause(
                transfer,
                "应用正在关闭，传输已暂停，可继续",
                notify_peer=False,
            )
        for record in list(self.state.outbound_files.values()):
            try:
                current_message = self.vault.get_message(record.message_id)
                if current_message is not None and current_message.get("status") in (
                    "sending",
                    "awaiting_consent",
                    "transferring",
                ):
                    file_meta = current_message.get("file") or {}
                    self.vault.update_message(
                        record.message_id,
                        status="paused",
                        error="应用正在关闭，传输已暂停，可继续",
                        file_update={
                            "paused_at": _now(),
                            "received_bytes": int(file_meta.get("received_bytes", 0) or 0),
                        },
                    )
            except Exception:
                log.debug("failed to pause outgoing transfer on shutdown", exc_info=True)