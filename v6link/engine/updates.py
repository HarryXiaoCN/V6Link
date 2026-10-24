"""Peer-to-peer application updates over the encrypted session channel.

Flow
----
1. The newer side runs ``push_update``: it connects, signs a manifest for its
   own program payload and sends ``update_offer``. The older peer validates the
   signature and only then shows the invitation to its user.
2. If the user accepts, the *receiver* connects back and sends
   ``update_pull``. The sender streams ``PAYLOAD_FILE`` frames, which keeps the
   receiver in control of when any bytes are fetched.
3. The receiver re-hashes the bytes, re-verifies the signature against the
   pinned contact identity and stages the file under ``updates/pending``. It
   never installs anything on its own.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..crypto import (
    PAYLOAD_CONTROL,
    PAYLOAD_FILE,
    EncryptedSession,
)
from ..errors import UpdateError
from ..updates import (
    MAX_UPDATE_BYTES,
    UPDATE_CHUNK,
    build_manifest,
    compare_versions,
    discard_part,
    ensure_dirs,
    normalize_manifest,
    part_path,
    payload_for_offer,
    running_executable,
    staged_path,
    verify_manifest,
)
from .models import (
    ACK_TIMEOUT,
    UPDATE_DATA_TIMEOUT,
    UPDATE_OFFER_TTL,
    _friendly_error,
    _monotonic,
    _now,
    _parse_control_payload,
    log,
)


@dataclass
class _OutgoingUpdate:
    offer_id: str
    contact_id: str
    path: Path
    manifest: dict[str, Any]
    expires: float

    @property
    def version(self) -> str:
        return str(self.manifest.get("version", ""))


@dataclass
class _IncomingUpdate:
    offer_id: str
    contact_id: str
    contact_name: str
    peer_id: str
    manifest: dict[str, Any]
    received_at: float
    accepted: bool = False
    task: asyncio.Task | None = field(default=None)

    @property
    def version(self) -> str:
        return str(self.manifest.get("version", ""))

    @property
    def size(self) -> int:
        return int(self.manifest.get("size", 0) or 0)


class _UpdateProgress:
    """Throttled ``update_progress`` events for one offer and direction."""

    def __init__(
        self,
        sink: Any,
        offer_id: str,
        contact_id: str,
        direction: str,
        total: int,
        *,
        interval: float = 0.12,
    ):
        self.sink = sink
        self.offer_id = offer_id
        self.contact_id = contact_id
        self.direction = direction
        self.total = max(0, int(total))
        self.interval = interval
        self.started = _monotonic()
        self.last_emit = 0.0

    def update(self, done: int, *, force: bool = False) -> None:
        done = max(0, min(int(done), self.total))
        now = _monotonic()
        if not force and now - self.last_emit < self.interval and done < self.total:
            return
        self.last_emit = now
        elapsed = max(0.001, now - self.started)
        payload = {
            "type": "update_progress",
            "offer_id": self.offer_id,
            "contact_id": self.contact_id,
            "direction": self.direction,
            "transferred": done,
            "total": self.total,
            "speed": done / elapsed,
            "percent": (done / self.total * 100.0) if self.total else 100.0,
        }
        try:
            self.sink(payload)
        except Exception:
            log.debug("update progress sink raised", exc_info=True)

    def finish(self) -> None:
        self.update(self.total, force=True)


class _UpdateMixin:
    """Offer, stream and stage peer-to-peer application updates."""

    # ------------------------------------------------------------- outgoing side

    def _prepare_local_payload(self, notes: str) -> tuple[Path, dict[str, Any]]:
        data_dir = self.vault.data_dir
        ensure_dirs(data_dir)
        path, kind, filename = payload_for_offer(data_dir, version=self.version)
        manifest = build_manifest(
            self.identity,
            path=path,
            kind=kind,
            filename=filename,
            notes=notes,
            version=self.version,
        )
        return path, manifest

    async def push_update(self, contact_id: str, notes: str = "") -> str:
        """Offer this device's program to a contact and wait for its answer."""
        contact = self._require_contact(contact_id)
        if contact.get("blocked"):
            raise ValueError("该好友已被拉黑")
        if not contact.get("peer_id"):
            raise UpdateError("该好友还没有完成身份认证，无法推送更新")
        self._prune_updates()
        path, manifest = await asyncio.to_thread(self._prepare_local_payload, notes)
        if manifest["size"] > MAX_UPDATE_BYTES:
            raise UpdateError("本机程序体积超出可推送上限")
        session: EncryptedSession | None = None
        ack: dict[str, Any] = {}
        try:
            session, reply = await self._open_peer_connection(contact)
            peer_version = str(reply.get("version") or "").strip()
            if peer_version and compare_versions(peer_version, self.version) >= 0:
                raise UpdateError(
                    f"对方版本 {peer_version} 不低于本机 {self.version}，不需要更新"
                )
            await session.send_control({"type": "update_offer", "offer": manifest})
            async with asyncio.timeout(ACK_TIMEOUT):
                ack = await session.receive_control()
        finally:
            await self._close_session(session)
        if ack.get("type") == "error":
            raise UpdateError("对方版本不支持自动更新，请先让对方升级到本版本或更新版本")
        if ack.get("type") != "update_offer_ack":
            raise UpdateError("对方返回了无效的更新响应")
        if not ack.get("ok"):
            raise UpdateError(str(ack.get("error") or "对方没有接受本次更新邀请"))
        self._outgoing_updates[manifest["offer_id"]] = _OutgoingUpdate(
            offer_id=manifest["offer_id"],
            contact_id=contact["id"],
            path=path,
            manifest=manifest,
            expires=_now() + UPDATE_OFFER_TTL,
        )
        self._emit(
            "update_invited",
            offer_id=manifest["offer_id"],
            contact_id=contact["id"],
            contact_name=contact.get("name", "好友"),
            version=self.version,
            size=manifest["size"],
        )
        return manifest["offer_id"]

    async def _handle_incoming_update_decline(
        self,
        session: EncryptedSession,
        contact: dict[str, Any],
        command: dict[str, Any],
    ) -> None:
        offer_id = str(command.get("offer_id") or "")
        record = self._outgoing_updates.pop(offer_id, None)
        with contextlib.suppress(Exception):
            await session.send_control({"type": "update_decline_ack", "ok": True})
        if record is None or record.contact_id != contact.get("id"):
            return
        self._emit(
            "update_declined",
            offer_id=offer_id,
            contact_id=contact.get("id", ""),
            contact_name=contact.get("name", "好友"),
            version=record.version,
            reason=str(command.get("reason") or "declined")[:80],
        )

    async def _handle_incoming_update_pull(
        self,
        session: EncryptedSession,
        contact: dict[str, Any],
        command: dict[str, Any],
    ) -> None:
        offer_id = str(command.get("offer_id") or "")
        record = self._outgoing_updates.get(offer_id)
        if record is None or record.contact_id != contact.get("id"):
            await session.send_control(
                {"type": "error", "error": "更新邀请已失效，请让对方重新推送"}
            )
            return
        if record.expires <= _now():
            self._outgoing_updates.pop(offer_id, None)
            await session.send_control({"type": "error", "error": "更新邀请已过期"})
            return
        try:
            size = record.path.stat().st_size
        except OSError:
            size = 0
        if size <= 0:
            self._outgoing_updates.pop(offer_id, None)
            await session.send_control({"type": "error", "error": "本机更新包已不可用"})
            return
        await session.send_control(
            {
                "type": "update_data_begin",
                "offer_id": offer_id,
                "size": size,
                "sha256": record.manifest["sha256"],
                "version": record.version,
            }
        )
        progress = _UpdateProgress(
            self._event_sink,
            offer_id,
            contact["id"],
            "send",
            size,
        )
        sent = 0
        try:
            with open(record.path, "rb") as handle:
                while True:
                    chunk = await asyncio.to_thread(handle.read, UPDATE_CHUNK)
                    if not chunk:
                        break
                    await session.send_file_chunk(sent, chunk)
                    sent += len(chunk)
                    progress.update(sent)
        except (ConnectionError, OSError):
            log.debug("update stream aborted by peer")
            self._emit(
                "update_failed",
                offer_id=offer_id,
                contact_id=contact.get("id", ""),
                contact_name=contact.get("name", "好友"),
                direction="send",
                message="对方在接收更新包时断开连接",
            )
            return
        progress.finish()
        self._outgoing_updates.pop(offer_id, None)
        with contextlib.suppress(Exception):
            await session.send_control(
                {"type": "update_data_end", "offer_id": offer_id, "size": sent}
            )
        self._emit(
            "update_pushed",
            offer_id=offer_id,
            contact_id=contact.get("id", ""),
            contact_name=contact.get("name", "好友"),
            version=record.version,
            size=sent,
        )

    # ------------------------------------------------------------- incoming side

    async def _handle_incoming_update_offer(
        self,
        session: EncryptedSession,
        contact: dict[str, Any],
        command: dict[str, Any],
    ) -> None:
        self._prune_updates()
        try:
            manifest = normalize_manifest(command.get("offer"))
            verify_manifest(manifest, session.remote.public_bytes)
        except UpdateError as exc:
            await session.send_control(
                {"type": "update_offer_ack", "ok": False, "error": str(exc)}
            )
            return
        if compare_versions(manifest["version"], self.version) <= 0:
            await session.send_control(
                {
                    "type": "update_offer_ack",
                    "ok": False,
                    "error": f"本机版本 {self.version} 不早于邀请中的 {manifest['version']}",
                }
            )
            return
        record = _IncomingUpdate(
            offer_id=manifest["offer_id"],
            contact_id=contact["id"],
            contact_name=str(contact.get("name", "好友")),
            peer_id=str(contact.get("peer_id", "")),
            manifest=manifest,
            received_at=_now(),
        )
        self._incoming_updates[manifest["offer_id"]] = record
        await session.send_control(
            {"type": "update_offer_ack", "ok": True, "offer_id": manifest["offer_id"]}
        )
        self._emit(
            "update_offer_received",
            offer={
                "offer_id": manifest["offer_id"],
                "contact_id": contact["id"],
                "contact_name": record.contact_name,
                "peer_id": record.peer_id,
                "version": manifest["version"],
                "current_version": self.version,
                "size": manifest["size"],
                "sha256": manifest["sha256"],
                "kind": manifest["kind"],
                "filename": manifest["filename"],
                "notes": manifest["notes"],
                "signature": manifest["signature"],
            },
        )

    async def respond_update_offer(self, offer_id: str, accepted: bool) -> None:
        """Accept (start pulling) or decline a previously received offer."""
        self._prune_updates()
        record = self._incoming_updates.get(offer_id)
        if record is None:
            raise UpdateError("更新邀请已失效，请让对方重新推送")
        contact = self._require_contact(record.contact_id)
        if accepted:
            if record.accepted:
                raise UpdateError("该更新已经在接收中")
            record.accepted = True
            record.task = self._spawn(
                self._pull_update(record, contact),
                name=f"update-pull-{offer_id[:8]}",
            )
            return
        self._incoming_updates.pop(offer_id, None)
        self._spawn(
            self._notify_update_declined(record, contact),
            name=f"update-decline-{offer_id[:8]}",
        )

    async def cancel_update(self, offer_id: str) -> None:
        """Stop an in-flight pull and delete whatever was already written."""
        record = self._incoming_updates.pop(offer_id, None)
        if record is None:
            return
        task = record.task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        discard_part(self.vault.data_dir, offer_id)
        self._emit(
            "update_canceled",
            offer_id=offer_id,
            contact_id=record.contact_id,
            contact_name=record.contact_name,
        )

    async def _notify_update_declined(
        self,
        record: _IncomingUpdate,
        contact: dict[str, Any],
    ) -> None:
        session: EncryptedSession | None = None
        try:
            session, _reply = await self._open_peer_connection(contact)
            await session.send_control(
                {"type": "update_decline", "offer_id": record.offer_id}
            )
            with contextlib.suppress(Exception):
                async with asyncio.timeout(ACK_TIMEOUT):
                    await session.receive_control()
        except Exception:
            log.debug("failed to notify update decline", exc_info=True)
        finally:
            await self._close_session(session)

    async def _pull_update(
        self,
        record: _IncomingUpdate,
        contact: dict[str, Any],
    ) -> None:
        data_dir = self.vault.data_dir
        session: EncryptedSession | None = None
        part = part_path(data_dir, record.offer_id)
        try:
            ensure_dirs(data_dir)
            session, _reply = await self._open_peer_connection(contact)
            await session.send_control(
                {
                    "type": "update_pull",
                    "offer_id": record.offer_id,
                    "version": record.version,
                }
            )
            async with asyncio.timeout(ACK_TIMEOUT):
                begin = await session.receive_control()
            if begin.get("type") == "error":
                raise UpdateError(str(begin.get("error") or "对方无法继续本次更新"))
            if begin.get("type") != "update_data_begin":
                raise UpdateError("对方返回了无效的更新响应")
            if begin.get("offer_id") != record.offer_id:
                raise UpdateError("更新响应与邀请不匹配")
            if int(begin.get("size", -1)) != record.size:
                raise UpdateError("更新包大小与邀请不一致")
            if str(begin.get("sha256", "")).lower() != record.manifest["sha256"]:
                raise UpdateError("更新包校验值与邀请不一致")

            progress = _UpdateProgress(
                self._event_sink,
                record.offer_id,
                contact["id"],
                "receive",
                record.size,
            )
            digest = hashlib.sha256()
            written = 0
            with open(part, "wb") as handle:
                while written < record.size:
                    async with asyncio.timeout(UPDATE_DATA_TIMEOUT):
                        kind_code, body = await session.receive_payload()
                    if kind_code == PAYLOAD_FILE:
                        if len(body) < 9:
                            raise UpdateError("更新数据块格式无效")
                        offset = struct.unpack(">Q", body[:8])[0]
                        chunk = body[8:]
                        if offset != written:
                            raise UpdateError("更新数据块顺序异常")
                        if written + len(chunk) > record.size:
                            raise UpdateError("更新包超出声明大小")
                        handle.write(chunk)
                        digest.update(chunk)
                        written += len(chunk)
                        progress.update(written)
                        continue
                    if kind_code == PAYLOAD_CONTROL:
                        message = _parse_control_payload(body)
                        if message.get("type") == "update_data_end":
                            break
                        raise UpdateError("更新过程中收到意外的控制消息")
                    raise UpdateError("更新过程中收到未知数据类型")
            if written != record.size:
                raise UpdateError("更新包不完整，请重新接收")
            if digest.hexdigest() != record.manifest["sha256"]:
                raise UpdateError("更新包校验失败，请重新接收")
            verify_manifest(record.manifest, session.remote.public_bytes)
            final = staged_path(data_dir, record.manifest["filename"])
            os.replace(part, final)
            progress.finish()
            self._incoming_updates.pop(record.offer_id, None)
            self._emit(
                "update_downloaded",
                offer_id=record.offer_id,
                contact_id=contact["id"],
                contact_name=record.contact_name,
                version=record.version,
                current_version=self.version,
                sha256=record.manifest["sha256"],
                size=record.size,
                kind=record.manifest["kind"],
                filename=record.manifest["filename"],
                path=str(final),
                installable=running_executable() is not None,
            )
        except Exception as exc:
            with contextlib.suppress(OSError):
                part.unlink(missing_ok=True)
            discard_part(data_dir, record.offer_id)
            self._incoming_updates.pop(record.offer_id, None)
            self._emit(
                "update_failed",
                offer_id=record.offer_id,
                contact_id=contact["id"],
                contact_name=record.contact_name,
                direction="receive",
                message=_friendly_error(exc),
            )
        finally:
            await self._close_session(session)

    # ------------------------------------------------------------------ helpers

    def _prune_updates(self) -> None:
        now = _now()
        for offer_id, record in list(self._outgoing_updates.items()):
            if record.expires <= now:
                self._outgoing_updates.pop(offer_id, None)
        for offer_id, incoming in list(self._incoming_updates.items()):
            if incoming.received_at + UPDATE_OFFER_TTL <= now and not incoming.accepted:
                self._incoming_updates.pop(offer_id, None)
        with contextlib.suppress(OSError):
            ensure_dirs(self.vault.data_dir)
