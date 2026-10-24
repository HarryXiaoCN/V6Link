"""ChatEngine facade: assembles services, lifecycle and public API."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..vault import Vault
from .connections import ConnectionManager
from .contacts import ContactService
from .events import EventBus
from .files import FileTransferService
from .messaging import MessagingService
from .models import APP_VERSION, EventSink
from .router import CommandRouter
from .tasks import TaskRegistry
from .transport import TcpTransport
from .updates import UpdateService


class ChatEngine:
    """Public entry point that owns the services and coordinates them."""

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
        self.identity = vault.identity()
        self.version = str(version or APP_VERSION)
        self.bind_host = bind_host
        profile = vault.profile()
        settings = vault.settings()
        requested = listen_port if listen_port is not None else profile.get("listen_port", 4455)
        if downloads_dir is not None:
            downloads_path = Path(downloads_dir).expanduser()
        else:
            configured_downloads = str(settings.get("downloads_dir", "") or "").strip()
            downloads_path = (
                Path(configured_downloads).expanduser()
                if configured_downloads
                else Path.home() / "Downloads"
            )
        self._started = False
        self._stopping = False

        self.events = EventBus(event_sink)
        self.tasks = TaskRegistry()
        self.transport = TcpTransport()
        self.contacts = ContactService(vault, self.events)
        self.connections = ConnectionManager(
            vault=vault,
            identity=self.identity,
            version=self.version,
            bind_host=bind_host,
            events=self.events,
            tasks=self.tasks,
            transport=self.transport,
            contacts=self.contacts,
            port=int(requested),
        )
        self.messaging = MessagingService(
            vault=vault,
            events=self.events,
            tasks=self.tasks,
            contacts=self.contacts,
            connections=self.connections,
            retry_dispatcher=self.retry_message,
        )
        self.files = FileTransferService(
            vault=vault,
            events=self.events,
            tasks=self.tasks,
            contacts=self.contacts,
            connections=self.connections,
            downloads_dir=downloads_path,
            schedule_waiting_retry=self.messaging.schedule_waiting_retry,
        )
        self.updates = UpdateService(
            vault=vault,
            identity=self.identity,
            version=self.version,
            events=self.events,
            tasks=self.tasks,
            contacts=self.contacts,
            connections=self.connections,
        )
        self.router = CommandRouter(
            messaging=self.messaging,
            files=self.files,
            updates=self.updates,
        )
        self.connections.command_handler = self.router.handle

    # ------------------------------------------------------------- properties

    @property
    def port(self) -> int:
        return self.connections.port

    @port.setter
    def port(self, value: int) -> None:
        self.connections.port = int(value)

    @property
    def _downloads_dir(self) -> Path:
        return self.files.downloads_dir

    @_downloads_dir.setter
    def _downloads_dir(self, value: str | Path) -> None:
        self.files.set_downloads_dir(value)

    @property
    def is_running(self) -> bool:
        return self._started

    # -------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        if self._started:
            return
        await self.connections.start_listener(self.port)
        self._started = True
        self.events.emit(
            "network",
            listening=True,
            port=self.port,
            host=self.bind_host,
        )

    async def stop(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        await self.connections.stop_listener()
        await self.files.pause_all()
        await self.tasks.cancel_all()
        await self.connections.close_all_sessions()
        self.events.emit("network", listening=False, port=self.port, host=self.bind_host)
        self._started = False

    def get_network_snapshot(self) -> dict[str, Any]:
        return self.connections.get_network_snapshot()

    def snapshot(self) -> dict[str, Any]:
        data = self.contacts.snapshot()
        data["network"] = self.connections.get_network_snapshot()
        return data

    # ---------------------------------------------------------------- contacts

    async def add_contact(
        self,
        name: str,
        peer_id: str = "",
        addresses: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return await self.contacts.add_contact(name, peer_id=peer_id, addresses=addresses)

    async def update_contact(self, contact_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        return await self.contacts.update_contact(contact_id, changes)

    async def delete_contact(self, contact_id: str) -> None:
        await self.contacts.delete_contact(contact_id)

    async def accept_pending_request(self, request_id: str) -> dict[str, Any]:
        contact, created = await self.contacts.accept_pending_request(request_id)
        if created:
            self.tasks.spawn(
                self.messaging.notify_friend_accepted(contact),
                name="friend-accept-notify",
            )
        return contact

    async def decline_pending_request(self, request_id: str) -> None:
        await self.contacts.decline_pending_request(request_id)

    def _match_incoming_peer(
        self,
        peer_id: str,
        source_host: str,
        advertised_port: int,
        peer_name: str,
        announced_hosts: list[str] | None = None,
    ) -> tuple[dict[str, Any] | None, bool, str, dict[str, Any] | None]:
        return self.contacts.match_incoming_peer(
            peer_id,
            source_host,
            advertised_port,
            peer_name,
            announced_hosts,
        )
    # --------------------------------------------------------------- messaging

    async def send_text(self, contact_id: str, text: str) -> str:
        return await self.messaging.send_text(contact_id, text)

    # ------------------------------------------------------------------- files

    async def send_file(self, contact_id: str, file_path: str | Path) -> str:
        return await self.files.send_file(contact_id, file_path)

    async def retry_message(self, message_id: str) -> None:
        message = self.vault.get_message(message_id)
        if message is None:
            raise ValueError("消息不存在")
        if message.get("direction") != "out":
            raise ValueError("只能重试已发送的消息")
        if message.get("kind") == "text":
            await self.messaging.retry_text(message_id)
            return
        await self.files.retry_file(message_id)

    async def respond_file_offer(self, file_id: str, accept: bool) -> None:
        await self.files.respond_file_offer(file_id, accept)

    async def cancel_transfer(self, message_id: str) -> None:
        await self.files.cancel_transfer(message_id)

    async def export_attachment(self, message_id: str, destination: str | Path) -> Path:
        return await self.files.export_attachment(message_id, destination)

    async def open_attachment(self, message_id: str) -> Path:
        return await self.files.open_attachment(message_id)

    # ----------------------------------------------------------------- updates

    async def push_update(self, contact_id: str, notes: str = "") -> str:
        return await self.updates.push_update(contact_id, notes)

    async def respond_update_offer(self, offer_id: str, accepted: bool) -> None:
        await self.updates.respond_update_offer(offer_id, accepted)

    async def cancel_update(self, offer_id: str) -> None:
        await self.updates.cancel_update(offer_id)

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
        listener_down = not self.connections.listening
        if new_port != old_port or listener_down:
            await self.connections.restart_listener(new_port, old_port)
            self._started = True
        self.vault.update_profile(name, new_port, parallel_streams)
        if target_downloads is not None and stored_downloads is not None:
            self._downloads_dir = target_downloads
            self.vault.set_downloads_dir(stored_downloads)
        self.events.emit(
            "network",
            listening=True,
            port=self.port,
            host=self.bind_host,
        )
        self.events.emit("profile_updated", profile=self.vault.profile(), settings=self.vault.settings())