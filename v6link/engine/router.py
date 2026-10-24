"""Dispatch incoming encrypted commands to the owning service."""

from __future__ import annotations

import asyncio
from typing import Any

from ..crypto import EncryptedSession

from .files import FileTransferService
from .messaging import MessagingService
from .models import CONTROL_TIMEOUT
from .updates import UpdateService


class CommandRouter:
    """Routes one incoming command frame to a service method."""

    def __init__(
        self,
        *,
        messaging: MessagingService,
        files: FileTransferService,
        updates: UpdateService,
    ):
        self.messaging = messaging
        self.files = files
        self.updates = updates

    async def handle(
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
            await self.messaging.handle_incoming_text(session, contact, command)
        elif command_type == "file_offer":
            await self.files.handle_offer(session, contact, command)
        elif command_type == "stream_open":
            await self.files.handle_data_stream(session, contact, command)
        elif command_type == "friend_accept":
            await self.messaging.handle_friend_accept(session, contact, command)
        elif command_type == "update_offer":
            await self.updates.handle_incoming_update_offer(session, contact, command)
        elif command_type == "update_pull":
            await self.updates.handle_incoming_update_pull(session, contact, command)
        elif command_type == "update_decline":
            await self.updates.handle_incoming_update_decline(session, contact, command)
        elif command_type == "ping":
            await session.send_control({"type": "pong"})
        else:
            await session.send_control({"type": "error", "error": "unsupported_command"})