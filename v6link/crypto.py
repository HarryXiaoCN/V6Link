"""Authenticated key exchange and encrypted framing for every V6Link socket.

Design
------
* Long-term identity: Ed25519 key pair. The public-key SHA-256 digest is the
  contact identity / fingerprint shown to users.
* Every TCP connection performs a fresh handshake:
     client -> client-hello  (nonce, X25519 ephemeral pubkey, identity, signature)
     server -> server-hello  (nonce, X25519 ephemeral pubkey, identity, signature)
     client -> first AES-GCM frame proves possession of the ephemeral key
* Session keys are derived with HKDF-SHA256 from the X25519 shared secret and
  the complete handshake transcript.
* All subsequent frames, including file chunks, are AES-256-GCM encrypted with
  independent per-direction keys and monotonically increasing nonces.
* There is no plaintext application data on the wire.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import struct
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .errors import HandshakeError

PROTOCOL_VERSION = 1
CLIENT_HELLO_MAGIC = b"V6LC"
SERVER_HELLO_MAGIC = b"V6LS"
HANDSHAKE_TIMEOUT = 10.0

CLIENT_SIGN_CONTEXT = b"V6Link/client-hello/v1\x00"
SERVER_SIGN_CONTEXT = b"V6Link/server-hello/v1\x00"
SESSION_KDF_INFO_PREFIX = b"V6Link/session/v1/"
NONCE_PREFIX = b"\x00\x00\x00\x00"

CLIENT_HELLO_SIZE = 4 + 1 + 32 + 32 + 32 + 64
SERVER_HELLO_SIZE = 4 + 1 + 32 * 6 + 64

PAYLOAD_CONTROL = 1
PAYLOAD_FILE = 2
MAX_FILE_CHUNK = 4 * 1024 * 1024
MAX_PLAINTEXT_FRAME = 1 + 8 + MAX_FILE_CHUNK
MAX_CIPHERTEXT_FRAME = MAX_PLAINTEXT_FRAME + 16
FRAME_AAD_PREFIX = b"V6Link/frame/v1\x00"


@dataclass(frozen=True)
class RemoteIdentity:
    public_bytes: bytes
    peer_id: str


class Identity:
    """A device's long-term Ed25519 identity key pair."""

    def __init__(self, private_key: ed25519.Ed25519PrivateKey):
        self._private = private_key
        self._public = private_key.public_key()
        self.public_bytes = self._public.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        self.peer_id = peer_id_for_public_key(self.public_bytes)

    @classmethod
    def generate(cls) -> Identity:
        return cls(ed25519.Ed25519PrivateKey.generate())

    @classmethod
    def from_private_bytes(cls, value: bytes) -> Identity:
        if len(value) != 32:
            raise ValueError("身份私钥长度无效")
        try:
            return cls(ed25519.Ed25519PrivateKey.from_private_bytes(value))
        except ValueError as exc:
            raise ValueError("身份私钥无效") from exc

    def private_bytes(self) -> bytes:
        return self._private.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )

    def sign(self, data: bytes) -> bytes:
        return self._private.sign(data)


def peer_id_for_public_key(public_bytes: bytes) -> str:
    if len(public_bytes) != 32:
        raise ValueError("身份公钥长度无效")
    return hashlib.sha256(b"V6Link identity v1\x00" + public_bytes).hexdigest()


def _load_public(raw: bytes) -> ed25519.Ed25519PublicKey:
    if len(raw) != 32:
        raise HandshakeError("身份公钥长度无效")
    try:
        return ed25519.Ed25519PublicKey.from_public_bytes(raw)
    except ValueError as exc:
        raise HandshakeError("身份公钥无效") from exc


def _verify(public_bytes: bytes, signature: bytes, payload: bytes) -> None:
    try:
        _load_public(public_bytes).verify(signature, payload)
    except InvalidSignature as exc:
        raise HandshakeError("身份签名验证失败") from exc


def _ephemeral_keypair() -> tuple[x25519.X25519PrivateKey, bytes]:
    private = x25519.X25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return private, public


def _exchange(private: x25519.X25519PrivateKey, peer_public: bytes) -> bytes:
    try:
        shared = private.exchange(x25519.X25519PublicKey.from_public_bytes(peer_public))
    except ValueError as exc:
        raise HandshakeError("密钥协商失败") from exc
    if not shared or not any(shared):
        raise HandshakeError("密钥协商产生了无效共享密钥")
    return shared


def _derive_keys(shared: bytes, transcript: bytes) -> tuple[bytes, bytes, bytes]:
    digest = hashlib.sha256(transcript).digest()
    salt = hashlib.sha256(b"V6Link/handshake-salt/v1\x00" + digest).digest()
    i2r = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=SESSION_KDF_INFO_PREFIX + b"initiator-to-responder",
    ).derive(shared)
    r2i = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=SESSION_KDF_INFO_PREFIX + b"responder-to-initiator",
    ).derive(shared)
    return i2r, r2i, digest


def _make_client_hello(identity: Identity) -> tuple[bytes, x25519.X25519PrivateKey, bytes]:
    nonce = os.urandom(32)
    eph_private, eph_public = _ephemeral_keypair()
    unsigned = (
        CLIENT_HELLO_MAGIC
        + bytes([PROTOCOL_VERSION])
        + nonce
        + eph_public
        + identity.public_bytes
    )
    signature = identity.sign(CLIENT_SIGN_CONTEXT + unsigned)
    return unsigned + signature, eph_private, nonce


def _make_server_hello(
    identity: Identity,
    client_hello: bytes,
    ephemeral_private: x25519.X25519PrivateKey,
    ephemeral_public: bytes,
) -> bytes:
    c_nonce = client_hello[5:37]
    c_eph = client_hello[37:69]
    c_id = client_hello[69:101]
    s_nonce = os.urandom(32)
    unsigned = (
        SERVER_HELLO_MAGIC
        + bytes([PROTOCOL_VERSION])
        + c_nonce
        + c_eph
        + c_id
        + s_nonce
        + ephemeral_public
        + identity.public_bytes
    )
    signature = identity.sign(SERVER_SIGN_CONTEXT + unsigned)
    return unsigned + signature


async def _read_exactly(reader: asyncio.StreamReader, count: int, label: str) -> bytes:
    try:
        return await reader.readexactly(count)
    except asyncio.IncompleteReadError as exc:
        raise HandshakeError(f"连接在{label}阶段中断") from exc


async def client_handshake(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    identity: Identity,
    expected_peer_id: str | None = None,
    timeout: float = HANDSHAKE_TIMEOUT,
) -> EncryptedSession:
    """Run the initiator side of the authenticated key exchange."""
    async with asyncio.timeout(timeout):
        client_hello, eph_private, _client_nonce = _make_client_hello(identity)
        writer.write(client_hello)
        await writer.drain()

        server_hello = await _read_exactly(reader, SERVER_HELLO_SIZE, "服务器响应")
        if server_hello[:4] != SERVER_HELLO_MAGIC:
            raise HandshakeError("对端不是 V6Link 服务或协议不兼容")
        if server_hello[4] != PROTOCOL_VERSION:
            raise HandshakeError("对端协议版本不兼容")
        c_nonce = server_hello[5:37]
        c_eph = server_hello[37:69]
        c_id = server_hello[69:101]
        s_eph = server_hello[133:165]
        s_id = server_hello[165:197]
        signature = server_hello[197:261]
        if c_nonce != client_hello[5:37] or c_eph != client_hello[37:69] or c_id != client_hello[69:101]:
            raise HandshakeError("握手响应绑定校验失败")
        unsigned = server_hello[:197]
        _verify(s_id, signature, SERVER_SIGN_CONTEXT + unsigned)

        peer_id = peer_id_for_public_key(s_id)
        if expected_peer_id and peer_id != expected_peer_id.lower():
            from .errors import IdentityMismatch

            raise IdentityMismatch("对方身份指纹与此前保存的不一致")

        shared = _exchange(eph_private, s_eph)
        transcript = client_hello + server_hello
        i2r, r2i, digest = _derive_keys(shared, transcript)
        session = EncryptedSession(
            reader=reader,
            writer=writer,
            send_key=i2r,
            recv_key=r2i,
            transcript_digest=digest,
            peer_public_bytes=s_id,
            peer_id=peer_id,
            is_initiator=True,
        )
        return session


async def server_handshake(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    identity: Identity,
    timeout: float = HANDSHAKE_TIMEOUT,
) -> EncryptedSession:
    """Run the responder side of the authenticated key exchange."""
    async with asyncio.timeout(timeout):
        client_hello = await _read_exactly(reader, CLIENT_HELLO_SIZE, "客户端握手")
        if client_hello[:4] != CLIENT_HELLO_MAGIC:
            raise HandshakeError("客户端握手魔数无效")
        if client_hello[4] != PROTOCOL_VERSION:
            raise HandshakeError("客户端协议版本不兼容")
        c_eph = client_hello[37:69]
        c_id = client_hello[69:101]
        c_signature = client_hello[101:165]
        _verify(c_id, c_signature, CLIENT_SIGN_CONTEXT + client_hello[:101])

        eph_private, eph_public = _ephemeral_keypair()
        server_hello = _make_server_hello(identity, client_hello, eph_private, eph_public)
        writer.write(server_hello)
        await writer.drain()

        shared = _exchange(eph_private, c_eph)
        transcript = client_hello + server_hello
        i2r, r2i, digest = _derive_keys(shared, transcript)
        peer_id = peer_id_for_public_key(c_id)
        return EncryptedSession(
            reader=reader,
            writer=writer,
            send_key=r2i,
            recv_key=i2r,
            transcript_digest=digest,
            peer_public_bytes=c_id,
            peer_id=peer_id,
            is_initiator=False,
        )


class EncryptedSession:
    """One authenticated TCP connection with encrypted, ordered frames."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        send_key: bytes,
        recv_key: bytes,
        transcript_digest: bytes,
        peer_public_bytes: bytes,
        peer_id: str,
        is_initiator: bool,
    ):
        self.reader = reader
        self.writer = writer
        self.peer_id = peer_id
        self.remote = RemoteIdentity(peer_public_bytes, peer_id)
        self.is_initiator = is_initiator
        self._send_aead = AESGCM(send_key)
        self._recv_aead = AESGCM(recv_key)
        self._aad = FRAME_AAD_PREFIX + transcript_digest
        self._send_seq = 0
        self._recv_seq = 0
        self._send_lock = asyncio.Lock()
        self.closed = False
        self.bytes_sent = 0
        self.bytes_received = 0

    @property
    def peername(self) -> tuple:
        info = self.writer.get_extra_info("peername")
        return info if isinstance(info, tuple) else ("", 0)

    def _nonce(self, sequence: int) -> bytes:
        return NONCE_PREFIX + struct.pack(">Q", sequence)

    async def send_control(self, payload: dict[str, Any]) -> None:
        await self._send_plain(PAYLOAD_CONTROL, _encode_json(payload))

    async def send_file_chunk(self, offset: int, data: bytes) -> None:
        if not 0 <= offset <= 0xFFFFFFFFFFFFFFFF:
            raise ValueError("文件块偏移无效")
        if len(data) > MAX_FILE_CHUNK:
            raise ValueError("文件块过大")
        await self._send_plain(PAYLOAD_FILE, struct.pack(">Q", offset) + data)

    async def _send_plain(self, kind: int, body: bytes) -> None:
        if self.closed:
            raise ConnectionError("连接已关闭")
        plaintext = bytes([kind]) + body
        if len(plaintext) > MAX_PLAINTEXT_FRAME:
            raise ValueError("加密帧超过大小限制")
        async with self._send_lock:
            sequence = self._send_seq
            self._send_seq += 1
            ciphertext = self._send_aead.encrypt(self._nonce(sequence), plaintext, self._aad)
            self.writer.write(struct.pack(">I", len(ciphertext)) + ciphertext)
            await self.writer.drain()
            self.bytes_sent += len(body)

    async def receive_payload(self) -> tuple[int, bytes]:
        header = await self.reader.readexactly(4)
        (length,) = struct.unpack(">I", header)
        if length < 17 or length > MAX_CIPHERTEXT_FRAME:
            raise HandshakeError("收到异常的加密帧长度")
        ciphertext = await self.reader.readexactly(length)
        sequence = self._recv_seq
        self._recv_seq += 1
        try:
            plaintext = self._recv_aead.decrypt(self._nonce(sequence), ciphertext, self._aad)
        except InvalidTag as exc:
            raise HandshakeError("消息完整性校验失败") from exc
        if not plaintext:
            raise HandshakeError("收到空的加密帧")
        self.bytes_received += max(0, len(plaintext) - 1)
        return plaintext[0], plaintext[1:]

    async def receive_control(self) -> dict[str, Any]:
        kind, body = await self.receive_payload()
        if kind != PAYLOAD_CONTROL:
            raise HandshakeError("期待控制消息，但收到其他类型数据")
        return _decode_json(body)

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.writer.close()
        with contextlib.suppress(Exception):
            await self.writer.wait_closed()


def _encode_json(payload: dict[str, Any]) -> bytes:
    try:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("控制消息无法序列化") from exc
    if len(encoded) > 512 * 1024:
        raise ValueError("控制消息过大")
    return encoded


def _decode_json(body: bytes) -> dict[str, Any]:
    if len(body) > 512 * 1024:
        raise HandshakeError("控制消息过大")
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HandshakeError("控制消息格式无效") from exc
    if not isinstance(value, dict) or not isinstance(value.get("type"), str):
        raise HandshakeError("控制消息缺少类型")
    return value

