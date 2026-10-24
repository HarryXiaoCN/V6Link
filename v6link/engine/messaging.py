"""Text messages, retries and friend-request notifications."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from ..crypto import (
    EncryptedSession,
)
from ..errors import (
    HandshakeError,
    PeerApprovalRequired,
    TransferError,
)

from .models import (
    ACK_TIMEOUT,
    log,
    _now,
    _OutboundFile,
    _friendly_error,
)


class _MessagingMixin:
    """Text messages, retries and friend-request notifications."""

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
