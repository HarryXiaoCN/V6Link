"""Contacts, pending requests, settings and state snapshots."""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any




class _ContactMixin:
    """Contacts, pending requests, settings and state snapshots."""

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
