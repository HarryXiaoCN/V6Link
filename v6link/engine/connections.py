"""Outgoing and incoming encrypted connections, peer matching and hello exchange."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import socket
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

from .models import (
    APP_WIRE_NAME,
    PROTOCOL,
    CONNECT_TIMEOUT,
    CONTROL_TIMEOUT,
    log,
    _clean_peer_name,
    _valid_announced_hosts,
    _validate_port,
)


class _ConnectionMixin:
    """Outgoing and incoming encrypted connections, peer matching and hello exchange."""

    @staticmethod
    def _configure_socket(writer: asyncio.StreamWriter) -> None:
        sock = writer.get_extra_info("socket")
        if sock is None:
            return
        with contextlib.suppress(OSError, AttributeError):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        with contextlib.suppress(OSError, AttributeError):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        if hasattr(socket, "TCP_KEEPIDLE"):
            with contextlib.suppress(OSError):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 30)
        if hasattr(socket, "TCP_KEEPINTVL"):
            with contextlib.suppress(OSError):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
        if hasattr(socket, "TCP_KEEPCNT"):
            with contextlib.suppress(OSError):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)

    async def _connect(self, host: str, port: int) -> EncryptedSession:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, family=socket.AF_INET6),
            timeout=CONNECT_TIMEOUT,
        )
        try:
            self._configure_socket(writer)
            session = await client_handshake(reader, writer, self.identity)
        except BaseException:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            raise
        self._sessions.add(session)
        return session

    async def _close_session(self, session: EncryptedSession | None) -> None:
        if session is None:
            return
        self._sessions.discard(session)
        await session.close()

    @staticmethod
    def _source_address(writer: asyncio.StreamWriter) -> tuple[str, int]:
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

    @staticmethod
    def _hello_payload() -> dict[str, Any]:
        return {
            "type": "hello",
            "app": APP_WIRE_NAME,
            "protocol": PROTOCOL,
        }

    def _local_profile_payload(self) -> dict[str, Any]:
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
        }

    async def _send_hello_and_wait(self, session: EncryptedSession) -> dict[str, Any]:
        payload = self._hello_payload()
        payload.update(self._local_profile_payload())
        await session.send_control(payload)
        async with asyncio.timeout(CONTROL_TIMEOUT):
            reply = await session.receive_control()
        if reply.get("type") != "hello_ack":
            raise HandshakeError("对端没有返回有效的好友验证结果")
        return reply

    @staticmethod
    def _advertised_peer_port(reply: dict[str, Any]) -> int:
        try:
            port = int(reply.get("listen_port", 0))
            if 1 <= port <= 65535:
                return port
        except (TypeError, ValueError):
            pass
        return 0

    async def _open_peer_connection(self, contact: dict[str, Any]) -> tuple[EncryptedSession, dict[str, Any]]:
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
                session = await self._connect(host, port)
            except (TimeoutError, OSError, HandshakeError, ValueError) as exc:
                errors.append(f"{format_endpoint(host, port) if host else host}: {exc}")
                continue
            try:
                if session.peer_id == self.identity.peer_id:
                    # A card or a hand-edited contact may still point at
                    # ::1. Never display this as a peer identity change.
                    await self._close_session(session)
                    self_skipped += 1
                    errors.append(
                        f"{format_endpoint(host, port)}: 该地址指向本机自身，已跳过"
                    )
                    continue
                if expected_peer_id and session.peer_id != expected_peer_id:

                    await self._close_session(session)
                    identity_conflict = True
                    self._emit(
                        "security_alert",
                        contact_id=contact.get("id", ""),
                        expected=expected_peer_id,
                        actual=session.peer_id,
                        message="对方身份指纹与此前固定的指纹不一致，已阻止本次连接。如确认对方重装或更换了数据目录，请核对新指纹后重新添加联系人。",
                    )
                    continue
                try:
                    async with asyncio.timeout(CONTROL_TIMEOUT):
                        reply = await self._send_hello_and_wait(session)
                except Exception:
                    await self._close_session(session)
                    raise
                accepted = bool(reply.get("accepted"))
                learned_peer = session.peer_id
                current_contact = self.vault.get_contact(contact["id"])
                if current_contact is None:
                    await self._close_session(session)
                    raise PeerUnreachable("好友已被删除")
                if not current_contact.get("peer_id"):
                    current_contact = self.vault.bind_peer_identity(contact["id"], learned_peer)
                    self._emit_contact(current_contact, force=True)
                else:
                    expected = current_contact.get("peer_id")
                    if expected != learned_peer:
                        await self._close_session(session)
                        identity_conflict = True
                        self._emit(
                            "security_alert",
                            contact_id=contact.get("id", ""),
                            expected=expected,
                            actual=learned_peer,
                            message="对方身份指纹与此前固定的指纹不一致，已阻止本次连接。如确认对方重装或更换了数据目录，请核对新指纹后重新添加联系人。",
                        )
                        continue
                if not accepted:
                    reason = str(reply.get("reason", ""))
                    await self._close_session(session)
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
                    advertised = self._advertised_peer_port(reply)
                    if advertised and (advertised != port):
                        updated = self.vault.add_or_touch_address(
                            contact["id"],
                            host,
                            advertised,
                            source="auto",
                            success=True,
                        )
                    announced_hosts = _valid_announced_hosts(reply.get("addresses"))
                    updated = self._add_announced_addresses(
                        updated,
                        announced_hosts,
                        advertised or port,
                    )
                    self._emit_contact(updated, force=True)
                except (ValueError, OSError) as exc:
                    log.debug("address book update failed: %s", exc)
                return session, reply
            except PeerApprovalRequired:
                raise
            except PeerUnreachable:
                raise
            except Exception as exc:
                await self._close_session(session)
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

    def get_network_snapshot(self) -> dict[str, Any]:
        return {
            "listening": self._started and self._server is not None,
            "port": self.port,
            "host": self.bind_host,
        }

    async def _handle_incoming_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        session: EncryptedSession | None = None
        current = asyncio.current_task()
        if current is not None:
            self._tasks.add(current)
        try:
            async with asyncio.timeout(HANDSHAKE_TIMEOUT + 2):
                session = await server_handshake(reader, writer, self.identity)
            self._sessions.add(session)
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
            source_host, _source_port = self._source_address(writer)
            if not source_host:
                raise HandshakeError("无法识别来源 IPv6 地址")
            announced_hosts = _valid_announced_hosts(hello.get("addresses"))

            contact, accepted, reason, request = self._match_incoming_peer(
                session.peer_id,
                source_host,
                advertised_port,
                peer_name,
                announced_hosts,
            )
            ack = {
                "type": "hello_ack",
                "accepted": accepted,
                **self._local_profile_payload(),
            }
            if not accepted:
                ack["reason"] = reason
            await session.send_control(ack)

            if request is not None:
                pending = self.vault.list_pending_requests()
                self._emit("contact_request", request=request, pending=pending)
            if not accepted:
                if reason == "contact_request":
                    log.info("new encrypted contact request received")
                return
            if contact is None:
                raise HandshakeError("好友匹配失败")
            self._emit_contact(contact, force=True)
            await self._serve_peer_commands(session, contact)
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
                self._tasks.discard(current)
            await self._close_session(session)
            with contextlib.suppress(Exception):
                writer.close()

    def _match_incoming_peer(
        self,
        peer_id: str,
        source_host: str,
        advertised_port: int,
        peer_name: str,
        announced_hosts: list[str] | None = None,
    ) -> tuple[dict[str, Any] | None, bool, str, dict[str, Any] | None]:
        announced_hosts = _valid_announced_hosts(announced_hosts)
        contact = self.vault.find_contact_by_peer_id(peer_id)
        if contact is not None:
            if contact.get("blocked"):
                return contact, False, "blocked", None
            try:
                contact = self.vault.add_or_touch_address(
                    contact["id"],
                    source_host,
                    advertised_port,
                    source="auto",
                    success=True,
                )
            except (ValueError, OSError):
                self.vault.touch_contact(contact["id"])
                contact = self.vault.get_contact(contact["id"]) or contact
            contact = self._add_announced_addresses(contact, announced_hosts, advertised_port)
            return contact, True, "", None

        host_matches = self.vault.contacts_matching_host(source_host)
        if host_matches:
            conflicting = [
                item for item in host_matches
                if item.get("peer_id") and item.get("peer_id") != peer_id
            ]
            if conflicting:
                expected = conflicting[0].get("peer_id", "")
                self._emit(
                    "security_alert",
                    contact_id=conflicting[0].get("id", ""),
                    expected=expected,
                    actual=peer_id,
                    message="来自好友地址的连接使用了不同的身份指纹，已阻止。",
                )
                return conflicting[0], False, "identity_mismatch", None

            empty_matches = [item for item in host_matches if not item.get("peer_id")]
            chosen: dict[str, Any] | None = None
            if len(empty_matches) == 1:
                chosen = empty_matches[0]
            elif empty_matches:
                port_matches = []
                for item in empty_matches:
                    for address in item.get("addresses", []):
                        if int(address.get("port", 0)) == advertised_port:
                            port_matches.append(item)
                            break
                unique = {item.get("id"): item for item in port_matches}
                if len(unique) == 1:
                    chosen = next(iter(unique.values()))
            if chosen is not None:
                try:
                    chosen = self.vault.bind_peer_identity(chosen["id"], peer_id)
                    chosen = self.vault.add_or_touch_address(
                        chosen["id"],
                        source_host,
                        advertised_port,
                        source="auto",
                        success=True,
                    )
                    chosen = self._add_announced_addresses(
                        chosen,
                        announced_hosts,
                        advertised_port,
                    )
                    return chosen, True, "", None
                except (ValueError, OSError):
                    pass
            expected = empty_matches[0].get("peer_id") if empty_matches else ""
            self._emit(
                "security_alert",
                contact_id=(empty_matches[0].get("id") if empty_matches else ""),
                expected=expected,
                actual=peer_id,
                message="无法将新身份安全地绑定到已有好友，已拒绝连接。",
            )
            return empty_matches[0] if empty_matches else None, False, "identity_mismatch", None

        try:
            request = self.vault.add_pending_request(
                peer_id,
                peer_name,
                source_host,
                advertised_port,
            )
        except ValueError:
            return None, False, "invalid_identity", None
        return None, False, "contact_request", request

    def _add_announced_addresses(
        self,
        contact: dict[str, Any],
        hosts: list[str],
        port: int,
    ) -> dict[str, Any]:
        for host in hosts:
            try:
                contact = self.vault.add_or_touch_address(
                    contact["id"],
                    host,
                    port,
                    source="auto",
                    success=False,
                )
            except (ValueError, OSError):
                log.debug("failed to learn announced address %s", host, exc_info=True)
        return contact

    async def _serve_peer_commands(
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
            await self._handle_incoming_text(session, contact, command)
        elif command_type == "file_offer":
            await self._handle_incoming_file_offer(session, contact, command)
        elif command_type == "stream_open":
            await self._handle_incoming_data_stream(session, contact, command)
        elif command_type == "friend_accept":
            await self._handle_friend_accept(session, contact, command)
        elif command_type == "ping":
            await session.send_control({"type": "pong"})
        else:
            await session.send_control({"type": "error", "error": "unsupported_command"})
