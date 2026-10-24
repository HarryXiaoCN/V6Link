"""Listener lifecycle and background task management."""

from __future__ import annotations

import asyncio
import contextlib
import socket
import sys
from collections.abc import Awaitable
from typing import Any


from .models import (
    log,
    _now,
)


class _LifecycleMixin:
    """Listener lifecycle and background task management."""

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
