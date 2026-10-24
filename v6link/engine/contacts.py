"""Contacts, pending requests and incoming peer matching."""

from __future__ import annotations

from typing import Any

from ..vault import Vault

from .events import EventBus
from .models import log, _valid_announced_hosts


class ContactService:
    """Owns contact CRUD and the identity/address matching rules."""

    def __init__(self, vault: Vault, events: EventBus):
        self.vault = vault
        self.events = events

    def require_contact(self, contact_id: str) -> dict[str, Any]:
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
        self.events.emit("contact_added", contact=contact)
        return contact

    async def update_contact(self, contact_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        contact = self.vault.update_contact(contact_id, **changes)
        self.events.emit("contact_updated", contact=contact)
        return contact

    async def delete_contact(self, contact_id: str) -> None:
        contact = self.vault.get_contact(contact_id)
        if contact is None:
            return
        self.vault.delete_contact(contact_id)
        self.events.emit("contact_deleted", contact_id=contact_id)

    async def accept_pending_request(self, request_id: str) -> tuple[dict[str, Any], bool]:
        """Create the contact for a pending request.

        Returns ``(contact, created)``. The facade decides whether a friend
        acceptance notification must be pushed to the peer.
        """
        request = self.vault.get_pending_request(request_id)
        if request is None:
            raise ValueError("好友请求已不存在")
        existing = self.vault.find_contact_by_peer_id(request["peer_id"])
        if existing is not None:
            self.vault.remove_pending_request(request_id)
            return existing, False
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
        self.events.emit("contact_added", contact=contact)
        self.events.emit("pending_requests_changed", pending=self.vault.list_pending_requests())
        return contact, True

    async def decline_pending_request(self, request_id: str) -> None:
        self.vault.remove_pending_request(request_id)
        self.events.emit("pending_requests_changed", pending=self.vault.list_pending_requests())

    def match_incoming_peer(
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
            contact = self.add_announced_addresses(contact, announced_hosts, advertised_port)
            return contact, True, "", None

        host_matches = self.vault.contacts_matching_host(source_host)
        if host_matches:
            conflicting = [
                item for item in host_matches
                if item.get("peer_id") and item.get("peer_id") != peer_id
            ]
            if conflicting:
                expected = conflicting[0].get("peer_id", "")
                self.events.emit(
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
                    chosen = self.add_announced_addresses(
                        chosen,
                        announced_hosts,
                        advertised_port,
                    )
                    return chosen, True, "", None
                except (ValueError, OSError):
                    pass
            expected = empty_matches[0].get("peer_id") if empty_matches else ""
            self.events.emit(
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

    def add_announced_addresses(
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

    def snapshot(self) -> dict[str, Any]:
        profile = self.vault.profile()
        return {
            "profile": profile,
            "settings": self.vault.settings(),
            "peer_id": self.vault.my_peer_id,
            "contacts": self.vault.list_contacts(),
            "pending_requests": self.vault.list_pending_requests(),
        }