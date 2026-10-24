"""Encrypted IPv6 connections, handshake and incoming command dispatch."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
from collections.abc import Awaitable, Callable
from typing import Any

from ..crypto import (
    HANDSHAKE_TIMEOUT,
    EncryptedSession,
    client_handshake,
    server_handshake,
)
from ..errors import (
    HandshakeError,
    PeerApprovalRequired,
    PeerUnreachable,
    V6LinkError,
)
from ..netutil import (
    format_endpoint,
    shareable_ipv6_addresses,
)

from .contacts import ContactService
from .events import EventBus
from .models import (
    APP_WIRE_NAME,
    PROTOCOL,
    CONTROL_TIMEOUT,
    log,
    _clean_peer_name,
    _valid_announced_hosts,
    _validate_port,
)
from .tasks import TaskRegistry
from .transport import Transport

CommandHandler = Callable[[EncryptedSession, dict[str, Any]], Awaitable[None]]


class ConnectionManager:
    """Owns the listener, live sessions and peer handshakes."""

    def __init__(
        self,
        *,
        vault: Any,
        identity: Any,
        version: str,
        bind_host: str,
        events: EventBus,
        tasks: TaskRegistry,
        transport: Transport,
        contacts: ContactService,
        port: int = 0,
        command_handler: CommandHandler | None = None,
    ):
        self.vault = vault
        self.identity = identity
        self.version = version
        self.bind_host = bind_host
        self.events = events
        self.tasks = tasks
        self.transport = transport
        self.contacts = contacts
        self.port = int(port)
        self.command_handler = command_handler
        self._server: asyncio.AbstractServer | None = None
        self.sessions: set[EncryptedSession] = set()
        self.started = False

    @property
    def listening(self) -> bool:
        return self._server is not None

    async def start_listener(self, port: int) -> None:
        server = await self.transport.listen(self.bind_host, port, self.handle_incoming_connection)
        self._server = server
        self.port = int(server.sockets[0].getsockname()[1])
        self.started = True

    async def stop_listener(self) -> None:
        server = self._server
        self._server = None
        if server is not None:
            await self.transport.close_server(server)
        self.started = False

    async def restart_listener(self, new_port: int, old_port: int) -> int:
        server = self._server
        self._server = None
        if server is not None:
            await self.transport.close_server(server)
        try:
            await self.start_listener(new_port)
            return self.port
        except OSError as exc:
            try:
                await self.start_listener(old_port)
                self.port = old_port
            except OSError:
                self._server = None
            raise ValueError(f"无法监听新端口：{exc.strerror or exc}") from exc

    async def close_all_sessions(self) -> None:
        for session in list(self.sessions):
            await session.close()
        self.sessions.clear()

    def get_network_snapshot(self) -> dict[str, Any]:
        return {
            "listening": self.started and self._server is not None,
            "port": self.port,
            "host": self.bind_host,
        }

    async def client_connect(self, host: str, port: int) -> EncryptedSession:
        reader, writer = await self.transport.open(host, port)
        try:
            session = await client_handshake(reader, writer, self.identity)
        except BaseException:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            raise
        self.sessions.add(session)
        return session

    async def close_session(self, session: EncryptedSession | None) -> None:
        if session is None:
            return
        self.sessions.discard(session)
        await session.close()

    def local_profile_payload(self) -> dict[str, Any]:
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
            "version": self.version,
        }

    @staticmethod
    def hello_payload() -> dict[str, Any]:
        return {
            "type": "hello",
            "app": APP_WIRE_NAME,
            "protocol": PROTOCOL,
        }

    async def send_hello_and_wait(self, session: EncryptedSession) -> dict[str, Any]:
        payload = self.hello_payload()
        payload.update(self.local_profile_payload())
        await session.send_control(payload)
        async with asyncio.timeout(CONTROL_TIMEOUT):
            reply = await session.receive_control()
        if reply.get("type") != "hello_ack":
            raise HandshakeError("对端没有返回有效的好友验证结果")
        return reply

    @staticmethod
    def advertised_peer_port(reply: dict[str, Any]) -> int:
        try:
            port = int(reply.get("listen_port", 0))
            if 1 <= port <= 65535:
                return port
        except (TypeError, ValueError):
            pass
        return 0

    @staticmethod
    def source_address(writer: asyncio.StreamWriter) -> tuple[str, int]:
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
    async def open_peer_connection(self, contact: dict[str, Any]) -> tuple[EncryptedSession, dict[str, Any]]:
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
                session = await self.client_connect(host, port)
            except (TimeoutError, OSError, HandshakeError, ValueError) as exc:
                errors.append(f"{format_endpoint(host, port) if host else host}: {exc}")
                continue
            try:
                if session.peer_id == self.identity.peer_id:
                    # A card or a hand-edited contact may still point at
                    # ::1. Never display this as a peer identity change.
                    await self.close_session(session)
                    self_skipped += 1
                    errors.append(
                        f"{format_endpoint(host, port)}: 该地址指向本机自身，已跳过"
                    )
                    continue
                if expected_peer_id and session.peer_id != expected_peer_id:

                    await self.close_session(session)
                    identity_conflict = True
                    self.events.emit(
                        "security_alert",
                        contact_id=contact.get("id", ""),
                        expected=expected_peer_id,
                        actual=session.peer_id,
                        message="对方身份指纹与此前固定的指纹不一致，已阻止本次连接。如确认对方重装或更换了数据目录，请核对新指纹后重新添加联系人。",
                    )
                    continue
                try:
                    async with asyncio.timeout(CONTROL_TIMEOUT):
                        reply = await self.send_hello_and_wait(session)
                except Exception:
                    await self.close_session(session)
                    raise
                accepted = bool(reply.get("accepted"))
                learned_peer = session.peer_id
                current_contact = self.vault.get_contact(contact["id"])
                if current_contact is None:
                    await self.close_session(session)
                    raise PeerUnreachable("好友已被删除")
                if not current_contact.get("peer_id"):
                    current_contact = self.vault.bind_peer_identity(contact["id"], learned_peer)
                    self.events.emit_contact(current_contact, force=True)
                else:
                    expected = current_contact.get("peer_id")
                    if expected != learned_peer:
                        await self.close_session(session)
                        identity_conflict = True
                        self.events.emit(
                            "security_alert",
                            contact_id=contact.get("id", ""),
                            expected=expected,
                            actual=learned_peer,
                            message="对方身份指纹与此前固定的指纹不一致，已阻止本次连接。如确认对方重装或更换了数据目录，请核对新指纹后重新添加联系人。",
                        )
                        continue
                if not accepted:
                    reason = str(reply.get("reason", ""))
                    await self.close_session(session)
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
                    advertised = self.advertised_peer_port(reply)
                    if advertised and (advertised != port):
                        updated = self.vault.add_or_touch_address(
                            contact["id"],
                            host,
                            advertised,
                            source="auto",
                            success=True,
                        )
                    announced_hosts = _valid_announced_hosts(reply.get("addresses"))
                    updated = self.contacts.add_announced_addresses(
                        updated,
                        announced_hosts,
                        advertised or port,
                    )
                    self.events.emit_contact(updated, force=True)
                except (ValueError, OSError) as exc:
                    log.debug("address book update failed: %s", exc)
                return session, reply
            except PeerApprovalRequired:
                raise
            except PeerUnreachable:
                raise
            except Exception as exc:
                await self.close_session(session)
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

    async def handle_incoming_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        session: EncryptedSession | None = None
        current = asyncio.current_task()
        if current is not None:
            self.tasks.track(current)
        try:
            async with asyncio.timeout(HANDSHAKE_TIMEOUT + 2):
                session = await server_handshake(reader, writer, self.identity)
            self.sessions.add(session)
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
            source_host, _source_port = self.source_address(writer)
            if not source_host:
                raise HandshakeError("无法识别来源 IPv6 地址")
            announced_hosts = _valid_announced_hosts(hello.get("addresses"))

            contact, accepted, reason, request = self.contacts.match_incoming_peer(
                session.peer_id,
                source_host,
                advertised_port,
                peer_name,
                announced_hosts,
            )
            ack = {
                "type": "hello_ack",
                "accepted": accepted,
                **self.local_profile_payload(),
            }
            if not accepted:
                ack["reason"] = reason
            await session.send_control(ack)

            if request is not None:
                pending = self.vault.list_pending_requests()
                self.events.emit("contact_request", request=request, pending=pending)
            if not accepted:
                if reason == "contact_request":
                    log.info("new encrypted contact request received")
                return
            if contact is None:
                raise HandshakeError("好友匹配失败")
            self.events.emit_contact(contact, force=True)
            if self.command_handler is None:
                raise HandshakeError("命令处理器尚未就绪")
            await self.command_handler(session, contact)
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
                self.tasks.untrack(current)
            await self.close_session(session)
            with contextlib.suppress(Exception):
                writer.close()