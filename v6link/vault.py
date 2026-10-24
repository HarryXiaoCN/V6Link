"""Encrypted, atomic local vault for identities, contacts, messages and keys."""

from __future__ import annotations

import base64
import copy
import getpass
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .crypto import Identity
from .errors import VaultError
from .keyvault import MasterKeyStore, atomic_write
from .netutil import normalize_ipv6_host

VAULT_MAGIC = b"V6VAULT1"
VAULT_VERSION = 1
VAULT_AAD = b"V6Link/local-vault-payload/v1"
MAX_TEXT_CHARS = 64 * 1024
MAX_ADDRESSES_PER_CONTACT = 24


def _now() -> float:
    return time.time()


def _clean_display_name(value: Any, fallback: str = "新联系人") -> str:
    text = str(value or "")
    text = "".join(ch if ch.isprintable() and ch not in "\t\r\n" else " " for ch in text)
    text = " ".join(text.split())
    return text[:40] or fallback


def _b64encode(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64decode(text: str) -> bytes:
    try:
        return base64.b64decode(text.encode("ascii"), validate=True)
    except Exception as exc:
        raise VaultError("保管库中的密钥字段损坏") from exc


def _valid_peer_id(peer_id: str) -> str:
    value = (peer_id or "").strip().lower()
    if value and not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("身份指纹格式无效")
    return value


def _normalize_address_entry(entry: dict[str, Any]) -> dict[str, Any]:
    host = normalize_ipv6_host(str(entry.get("host", "")))
    try:
        port = int(entry.get("port", 4455))
    except (TypeError, ValueError) as exc:
        raise ValueError("端口必须是数字") from exc
    if not 1 <= port <= 65535:
        raise ValueError("端口必须在 1–65535 之间")
    label = str(entry.get("label", "") or "").strip()[:40]
    try:
        last_success = float(entry.get("last_success", 0.0) or 0.0)
    except (TypeError, ValueError):
        last_success = 0.0
    source = entry.get("source", "manual")
    if source not in ("manual", "auto"):
        source = "manual"
    return {
        "host": host,
        "port": port,
        "label": label,
        "last_success": max(0.0, last_success),
        "source": source,
    }


def _dedupe_addresses(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[tuple[str, int], dict[str, Any]] = {}
    for entry in entries:
        normalized = _normalize_address_entry(entry)
        key = (normalized["host"].casefold(), normalized["port"])
        previous = unique.get(key)
        if previous is None:
            unique[key] = normalized
        else:
            # Keep the higher priority human label and latest success time.
            if not previous.get("label") and normalized.get("label"):
                previous["label"] = normalized["label"]
            previous["last_success"] = max(previous.get("last_success", 0.0), normalized.get("last_success", 0.0))
            if normalized.get("source") == "manual":
                previous["source"] = "manual"
    return list(unique.values())


def _default_state(display_name: str, listen_port: int) -> dict[str, Any]:
    identity = Identity.generate()
    return {
        "version": VAULT_VERSION,
        "created_at": _now(),
        "identity_private": _b64encode(identity.private_bytes()),
        "profile": {
            "display_name": _clean_display_name(display_name, "我"),
            "listen_port": int(listen_port),
        },
        "settings": {
            "parallel_streams": 4,
            "downloads_dir": "",
        },
        "contacts": [],
        "messages": [],
        "pending_requests": [],
    }


class Vault:
    """Application state, serialized as one AES-256-GCM encrypted file.

    All metadata (contact names, IPv6 addresses, messages, fingerprints, file
    names and per-file keys) lives inside the encrypted JSON payload. Small
    writes are atomic; attachments are separate encrypted containers.
    """

    def __init__(
        self,
        data_dir: str | Path,
        display_name: str | None = None,
        listen_port: int = 4455,
    ):
        self.root = Path(data_dir).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.attachments_dir = self.root / "attachments"
        self.attachments_dir.mkdir(parents=True, exist_ok=True)
        self.vault_path = self.root / "vault.v6"
        self._lock = threading.RLock()
        self._key = MasterKeyStore(self.root).load_or_create()
        self._aead = AESGCM(self._key)
        self._state = self._load_or_create(display_name, listen_port)
        self._recover_incomplete()
        self.save()

    # ------------------------------------------------------------------ core

    @property
    def data_dir(self) -> Path:
        return self.root

    def _load_or_create(self, display_name: str | None, listen_port: int) -> dict[str, Any]:
        if not self.vault_path.exists():
            state = _default_state(
                display_name or getpass.getuser() or "我",
                int(listen_port),
            )
            self._state = state
            self._save_locked()
            return state
        try:
            raw = self.vault_path.read_bytes()
        except OSError as exc:
            raise VaultError(f"无法读取加密保管库：{exc}") from exc
        if not raw.startswith(VAULT_MAGIC) or len(raw) < len(VAULT_MAGIC) + 1 + 12 + 16:
            raise VaultError("加密保管库文件格式无效")
        version = raw[len(VAULT_MAGIC)]
        if version != VAULT_VERSION:
            raise VaultError("加密保管库版本不受支持")
        nonce_start = len(VAULT_MAGIC) + 1
        nonce = raw[nonce_start : nonce_start + 12]
        ciphertext = raw[nonce_start + 12 :]
        try:
            plaintext = self._aead.decrypt(nonce, ciphertext, VAULT_AAD)
        except InvalidTag as exc:
            raise VaultError("本地数据解密失败：密钥不匹配或保管库已损坏") from exc
        try:
            state = json.loads(plaintext.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise VaultError("本地数据内容无效") from exc
        if not isinstance(state, dict) or state.get("version") != VAULT_VERSION:
            raise VaultError("本地数据版本不受支持")
        self._validate_state(state)
        return state

    @staticmethod
    def _validate_state(state: dict[str, Any]) -> None:
        required = ("identity_private", "profile", "settings", "contacts", "messages")
        if any(key not in state for key in required):
            raise VaultError("本地数据结构不完整")
        if not isinstance(state.get("contacts"), list) or not isinstance(state.get("messages"), list):
            raise VaultError("本地数据集合无效")
        state.setdefault("pending_requests", [])

    def _save_locked(self) -> None:
        payload = json.dumps(
            self._state,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        nonce = os.urandom(12)
        ciphertext = self._aead.encrypt(nonce, payload, VAULT_AAD)
        blob = VAULT_MAGIC + bytes([VAULT_VERSION]) + nonce + ciphertext
        try:
            atomic_write(self.vault_path, blob, mode=0o600)
        except OSError as exc:
            raise VaultError(f"无法写入加密保管库：{exc}") from exc

    def save(self) -> None:
        with self._lock:
            self._save_locked()

    def _recover_incomplete(self) -> None:
        """Recover interrupted transfers as resumable pauses, not deletions.

        A hard process exit cannot send a clean pause/cancel control frame.
        Keeping the authenticated attachment slots and metadata lets a later
        offer continue from the receiver's bitmap. Explicit user cancellation
        is handled by the engine and does delete the partial container.
        """
        changed = False
        for message in self._state["messages"]:
            if message.get("status") not in (
                "sending",
                "receiving",
                "awaiting_consent",
                "transferring",
            ):
                continue
            if message.get("kind") == "file":
                message["status"] = "paused"
                message["error"] = "应用上次关闭时传输已暂停，连接恢复后可继续"
                file_meta = message.setdefault("file", {})
                if isinstance(file_meta, dict):
                    file_meta["paused_at"] = _now()
            else:
                message["status"] = "failed"
                message["error"] = "应用上次关闭时消息尚未发送完成"
            changed = True
        if changed:
            self._save_locked()

    # ---------------------------------------------------------------- identity

    def identity(self) -> Identity:
        with self._lock:
            return Identity.from_private_bytes(_b64decode(self._state["identity_private"]))

    @property
    def my_peer_id(self) -> str:
        with self._lock:
            return self.identity().peer_id

    def profile(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._state["profile"])

    def settings(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._state["settings"])

    def update_profile(
        self,
        display_name: str,
        listen_port: int,
        parallel_streams: int | None = None,
    ) -> dict[str, Any]:
        name = _clean_display_name(display_name, "")
        if not name:
            raise ValueError("昵称不能为空")
        if len(name) > 40:
            raise ValueError("昵称不能超过 40 个字符")
        try:
            port = int(listen_port)
        except (TypeError, ValueError) as exc:
            raise ValueError("端口必须是数字") from exc
        if not 1 <= port <= 65535:
            raise ValueError("端口必须在 1–65535 之间")
        with self._lock:
            self._state["profile"]["display_name"] = name
            self._state["profile"]["listen_port"] = port
            if parallel_streams is not None:
                try:
                    workers = int(parallel_streams)
                except (TypeError, ValueError):
                    workers = 4
                self._state["settings"]["parallel_streams"] = max(1, min(8, workers))
            self._save_locked()
            return copy.deepcopy(self._state["profile"])

    def set_downloads_dir(self, path: str) -> None:
        with self._lock:
            self._state["settings"]["downloads_dir"] = str(path or "")
            self._save_locked()

    # ----------------------------------------------------------------- contacts

    def list_contacts(self) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._state["contacts"])

    def get_contact(self, contact_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._find_contact_locked(contact_id)

    def _find_contact_locked(self, contact_id: str) -> dict[str, Any] | None:
        for contact in self._state["contacts"]:
            if contact.get("id") == contact_id:
                return copy.deepcopy(contact)
        return None

    def find_contact_by_peer_id(self, peer_id: str) -> dict[str, Any] | None:
        peer_id = (peer_id or "").lower()
        with self._lock:
            for contact in self._state["contacts"]:
                if contact.get("peer_id") == peer_id:
                    return copy.deepcopy(contact)
        return None

    def contacts_matching_host(self, host: str) -> list[dict[str, Any]]:
        normalized = normalize_ipv6_host(host).casefold()
        with self._lock:
            matches = []
            for contact in self._state["contacts"]:
                for address in contact.get("addresses", []):
                    if str(address.get("host", "")).casefold() == normalized:
                        matches.append(copy.deepcopy(contact))
                        break
            return matches

    def create_contact(
        self,
        name: str,
        peer_id: str = "",
        addresses: list[dict[str, Any]] | None = None,
        verified: bool = False,
    ) -> dict[str, Any]:
        clean_name = _clean_display_name(name)
        clean_peer_id = _valid_peer_id(peer_id)
        clean_addresses = _dedupe_addresses(addresses or [])[:MAX_ADDRESSES_PER_CONTACT]
        with self._lock:
            if clean_peer_id and any(c.get("peer_id") == clean_peer_id for c in self._state["contacts"]):
                raise ValueError("该身份已在好友列表中")
            contact = {
                "id": uuid.uuid4().hex,
                "peer_id": clean_peer_id,
                "name": clean_name,
                "addresses": clean_addresses,
                "created_at": _now(),
                "last_seen": 0.0,
                "verified": bool(verified),
                "blocked": False,
            }
            self._state["contacts"].append(contact)
            self._state["pending_requests"] = [
                item for item in self._state.get("pending_requests", [])
                if item.get("peer_id") != clean_peer_id
            ]
            self._save_locked()
            return copy.deepcopy(contact)

    def update_contact(
        self,
        contact_id: str,
        *,
        name: str | None = None,
        peer_id: str | None = None,
        addresses: list[dict[str, Any]] | None = None,
        verified: bool | None = None,
        blocked: bool | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            contact = next((c for c in self._state["contacts"] if c.get("id") == contact_id), None)
            if contact is None:
                raise ValueError("联系人不存在")
            if name is not None:
                clean = _clean_display_name(name, "")
                if not clean:
                    raise ValueError("好友昵称不能为空")
                contact["name"] = clean
            if peer_id is not None:
                clean_peer_id = _valid_peer_id(peer_id)
                if clean_peer_id and any(
                    c.get("peer_id") == clean_peer_id and c.get("id") != contact_id
                    for c in self._state["contacts"]
                ):
                    raise ValueError("该身份已绑定到其他好友")
                contact["peer_id"] = clean_peer_id
            if addresses is not None:
                normalized = _dedupe_addresses(addresses)
                if len(normalized) > MAX_ADDRESSES_PER_CONTACT:
                    raise ValueError(f"每个好友最多保存 {MAX_ADDRESSES_PER_CONTACT} 个地址")
                contact["addresses"] = normalized
            if verified is not None:
                contact["verified"] = bool(verified)
            if blocked is not None:
                contact["blocked"] = bool(blocked)
            self._save_locked()
            return copy.deepcopy(contact)

    def delete_contact(self, contact_id: str) -> None:
        with self._lock:
            contact = next((c for c in self._state["contacts"] if c.get("id") == contact_id), None)
            if contact is None:
                return
            self._delete_attachments_locked(contact_id)
            self._state["contacts"] = [c for c in self._state["contacts"] if c.get("id") != contact_id]
            self._state["messages"] = [
                m for m in self._state["messages"] if m.get("contact_id") != contact_id
            ]
            self._save_locked()

    def _delete_attachments_locked(self, contact_id: str) -> None:
        for message in self._state["messages"]:
            if message.get("contact_id") != contact_id:
                continue
            file_meta = message.get("file") or {}
            relative = file_meta.get("enc_path")
            if relative:
                try:
                    (self.root / relative).unlink(missing_ok=True)
                except OSError:
                    pass

    def bind_peer_identity(self, contact_id: str, peer_id: str) -> dict[str, Any]:
        peer_id = _valid_peer_id(peer_id)
        if not peer_id:
            raise ValueError("身份指纹不能为空")
        with self._lock:
            contact = next((c for c in self._state["contacts"] if c.get("id") == contact_id), None)
            if contact is None:
                raise ValueError("联系人不存在")
            if contact.get("peer_id") and contact["peer_id"] != peer_id:
                raise ValueError("身份指纹与此前记录不一致")
            contact["peer_id"] = peer_id
            self._save_locked()
            return copy.deepcopy(contact)

    def add_or_touch_address(
        self,
        contact_id: str,
        host: str,
        port: int,
        *,
        source: str = "auto",
        success: bool = False,
        label: str = "",
    ) -> dict[str, Any]:
        entry = _normalize_address_entry(
            {"host": host, "port": port, "source": source, "label": label}
        )
        if success:
            entry["last_success"] = _now()
        with self._lock:
            contact = next((c for c in self._state["contacts"] if c.get("id") == contact_id), None)
            if contact is None:
                raise ValueError("联系人不存在")
            addresses: list[dict[str, Any]] = contact.setdefault("addresses", [])
            found = None
            for existing in addresses:
                if (
                    str(existing.get("host", "")).casefold() == entry["host"].casefold()
                    and int(existing.get("port", 0)) == entry["port"]
                ):
                    found = existing
                    break
            if found is None:
                addresses.append(entry)
                if len(addresses) > MAX_ADDRESSES_PER_CONTACT:
                    addresses.sort(
                        key=lambda item: (
                            item.get("source") == "manual",
                            float(item.get("last_success", 0.0) or 0.0),
                        )
                    )
                    del addresses[: len(addresses) - MAX_ADDRESSES_PER_CONTACT]
            else:
                if success:
                    found["last_success"] = _now()
                if not found.get("label") and entry.get("label"):
                    found["label"] = entry["label"]
                if entry.get("source") == "manual":
                    found["source"] = "manual"
            contact["last_seen"] = max(float(contact.get("last_seen", 0.0) or 0.0), _now())
            self._save_locked()
            return copy.deepcopy(contact)

    def touch_contact(self, contact_id: str, when: float | None = None) -> None:
        with self._lock:
            contact = next((c for c in self._state["contacts"] if c.get("id") == contact_id), None)
            if contact is None:
                return
            contact["last_seen"] = float(when or _now())
            self._save_locked()

    # ---------------------------------------------------------- pending requests

    def list_pending_requests(self) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._state.get("pending_requests", []))

    def get_pending_request(self, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            for item in self._state.get("pending_requests", []):
                if item.get("id") == request_id:
                    return copy.deepcopy(item)
        return None

    def add_pending_request(
        self,
        peer_id: str,
        name: str,
        host: str,
        port: int,
    ) -> dict[str, Any] | None:
        peer_id = _valid_peer_id(peer_id)
        if not peer_id:
            raise ValueError("身份指纹不能为空")
        host = normalize_ipv6_host(host)
        port = int(port)
        if not 1 <= port <= 65535:
            raise ValueError("端口无效")
        with self._lock:
            if any(contact.get("peer_id") == peer_id for contact in self._state["contacts"]):
                return None
            for existing in self._state.get("pending_requests", []):
                if existing.get("peer_id") == peer_id:
                    existing["name"] = _clean_display_name(name or existing.get("name"))
                    existing["host"] = host
                    existing["port"] = port
                    existing["last_seen"] = _now()
                    self._save_locked()
                    return None
            request = {
                "id": uuid.uuid4().hex,
                "peer_id": peer_id,
                "name": _clean_display_name(name),
                "host": host,
                "port": port,
                "created_at": _now(),
            }
            requests = self._state.setdefault("pending_requests", [])
            requests.append(request)
            if len(requests) > 30:
                del requests[: len(requests) - 30]
            self._save_locked()
            return copy.deepcopy(request)

    def remove_pending_request(self, request_id: str) -> None:
        with self._lock:
            self._state["pending_requests"] = [
                item for item in self._state.get("pending_requests", [])
                if item.get("id") != request_id
            ]
            self._save_locked()

    # ----------------------------------------------------------------- messages

    def list_messages(self, contact_id: str) -> list[dict[str, Any]]:
        with self._lock:
            messages = [
                copy.deepcopy(m)
                for m in self._state["messages"]
                if m.get("contact_id") == contact_id
            ]
        messages.sort(key=lambda item: (float(item.get("created_at", 0)), item.get("id", "")))
        return messages

    def get_message(self, message_id: str) -> dict[str, Any] | None:
        with self._lock:
            for message in self._state["messages"]:
                if message.get("id") == message_id:
                    return copy.deepcopy(message)
        return None

    def find_incoming_by_origin(self, contact_id: str, origin_id: str) -> dict[str, Any] | None:
        if not origin_id:
            return None
        with self._lock:
            for message in self._state["messages"]:
                if (
                    message.get("contact_id") == contact_id
                    and message.get("direction") == "in"
                    and message.get("origin_id") == origin_id
                ):
                    return copy.deepcopy(message)
        return None

    def add_text_message(
        self,
        contact_id: str,
        direction: str,
        text: str,
        status: str,
        *,
        origin_id: str = "",
        created_at: float | None = None,
    ) -> dict[str, Any]:
        clean_text = str(text or "")
        if not clean_text.strip():
            raise ValueError("消息不能为空")
        if len(clean_text) > MAX_TEXT_CHARS:
            raise ValueError("消息过长")
        if direction not in ("in", "out"):
            raise ValueError("消息方向无效")
        with self._lock:
            if self._find_contact_locked(contact_id) is None:
                raise ValueError("联系人不存在")
            message = {
                "id": uuid.uuid4().hex,
                "contact_id": contact_id,
                "direction": direction,
                "kind": "text",
                "text": clean_text,
                "file": None,
                "created_at": float(created_at or _now()),
                "status": status,
                "error": "",
                "origin_id": origin_id,
            }
            self._state["messages"].append(message)
            self._save_locked()
            return copy.deepcopy(message)

    def add_file_message(
        self,
        contact_id: str,
        direction: str,
        file_meta: dict[str, Any],
        status: str,
        *,
        origin_id: str = "",
        created_at: float | None = None,
    ) -> dict[str, Any]:
        if direction not in ("in", "out"):
            raise ValueError("消息方向无效")
        if not isinstance(file_meta, dict):
            raise ValueError("文件信息无效")
        with self._lock:
            if self._find_contact_locked(contact_id) is None:
                raise ValueError("联系人不存在")
            message = {
                "id": uuid.uuid4().hex,
                "contact_id": contact_id,
                "direction": direction,
                "kind": "file",
                "text": "",
                "file": copy.deepcopy(file_meta),
                "created_at": float(created_at or _now()),
                "status": status,
                "error": "",
                "origin_id": origin_id,
            }
            self._state["messages"].append(message)
            self._save_locked()
            return copy.deepcopy(message)

    def update_message(
        self,
        message_id: str,
        *,
        status: str | None = None,
        error: str | None = None,
        file_update: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            message = next((m for m in self._state["messages"] if m.get("id") == message_id), None)
            if message is None:
                raise ValueError("消息不存在")
            if status is not None:
                message["status"] = status
            if error is not None:
                message["error"] = error
            if file_update:
                file_meta = message.setdefault("file", {})
                if not isinstance(file_meta, dict):
                    file_meta = {}
                    message["file"] = file_meta
                file_meta.update(copy.deepcopy(file_update))
            self._save_locked()
            return copy.deepcopy(message)

    def pending_outgoing_messages(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                copy.deepcopy(message)
                for message in self._state["messages"]
                if message.get("direction") == "out"
                and message.get("status") in ("waiting", "failed")
            ]

    def messages_for_retry(self, contact_id: str, statuses: tuple[str, ...] = ("failed", "waiting")) -> list[dict[str, Any]]:
        with self._lock:
            return [
                copy.deepcopy(message)
                for message in self._state["messages"]
                if message.get("contact_id") == contact_id
                and message.get("direction") == "out"
                and message.get("status") in statuses
            ]

    # -------------------------------------------------------------- diagnostics

    def raw_state_copy(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._state)

    def attachment_path(self, relative: str | Path) -> Path:
        candidate = (self.root / relative).resolve()
        root = self.root.resolve()
        if candidate != root and root not in candidate.parents:
            raise VaultError("附件路径越界")
        return candidate



