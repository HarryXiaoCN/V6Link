"""Encrypted attachment containers with random-access chunk encryption.

Every chunk is independently AES-256-GCM sealed. Fixed chunk slots make
parallel, out-of-order receipt possible without ever writing plaintext to disk.
File names, sizes and per-file keys live only inside the encrypted vault.
"""

from __future__ import annotations

import atexit
import base64
import os
import struct
import tempfile
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .errors import VaultError
from .netutil import sanitize_filename

MAGIC = b"V6FILE01"
VERSION = 1
HEADER_STRUCT = struct.Struct(">8sB3xI8s8x")
HEADER_SIZE = HEADER_STRUCT.size
TAG_SIZE = 16
MIN_CHUNK = 64 * 1024
MAX_CHUNK = 4 * 1024 * 1024
DEFAULT_CHUNK = 1024 * 1024
AAD_PREFIX = b"V6Link/attachment/v1\x00"

_TEMP_LOCK = threading.Lock()
_TEMP_FILES: set[Path] = set()


def _cleanup_temp_files() -> None:
    with _TEMP_LOCK:
        paths = list(_TEMP_FILES)
        _TEMP_FILES.clear()
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


atexit.register(_cleanup_temp_files)


def _key_bytes(meta: dict[str, Any]) -> bytes:
    try:
        key = base64.b64decode(str(meta["key"]).encode("ascii"), validate=True)
    except Exception as exc:
        raise VaultError("附件密钥格式无效") from exc
    if len(key) != 32:
        raise VaultError("附件密钥长度无效")
    return key


def _attachment_nonce(prefix: bytes, index: int) -> bytes:
    return prefix + struct.pack(">I", index)


def _aad(chunk_size: int, prefix: bytes) -> bytes:
    return AAD_PREFIX + struct.pack(">BI", VERSION, int(chunk_size)) + prefix


def _chunk_plain_len(size: int, chunk_size: int, index: int) -> int:
    start = index * chunk_size
    return min(chunk_size, size - start)


def _chunk_count(size: int, chunk_size: int) -> int:
    if size <= 0:
        return 0
    return (size + chunk_size - 1) // chunk_size


def _container_size(size: int, chunk_size: int) -> int:
    if size <= 0:
        return HEADER_SIZE
    count = _chunk_count(size, chunk_size)
    return HEADER_SIZE + count * (chunk_size + TAG_SIZE) - (chunk_size - _chunk_plain_len(size, chunk_size, count - 1))


class AttachmentWriter:
    """Writes authenticated encrypted chunk slots to one container."""

    def __init__(
        self,
        root: Path,
        relative_path: str,
        key: bytes,
        size: int,
        chunk_size: int,
        prefix: bytes,
        file_obj,
    ):
        self.root = Path(root)
        self.relative_path = relative_path
        self.path = self.root / relative_path
        self.key = key
        self.size = int(size)
        self.chunk_size = int(chunk_size)
        self.prefix = prefix
        self._file = file_obj
        self._aead = AESGCM(key)
        self._aad = _aad(self.chunk_size, self.prefix)
        self._lock = threading.Lock()
        self._received: set[int] = set()
        self._received_bytes = 0
        self._finished = False
        self.expected_chunks = _chunk_count(self.size, self.chunk_size)

    @classmethod
    def create(
        cls,
        data_dir: Path,
        size: int,
        chunk_size: int = DEFAULT_CHUNK,
        *,
        container_id: str | None = None,
    ) -> AttachmentWriter:
        size = int(size)
        chunk_size = int(chunk_size)
        if size < 0 or size > 1024 * 1024 * 1024 * 1024:
            raise VaultError("文件大小无效")
        if not MIN_CHUNK <= chunk_size <= MAX_CHUNK:
            raise VaultError("文件分块大小无效")
        folder = Path(data_dir) / "attachments"
        folder.mkdir(parents=True, exist_ok=True)
        identifier = container_id or uuid.uuid4().hex
        relative = f"attachments/{identifier}.v6f"
        path = Path(data_dir) / relative
        key = os.urandom(32)
        prefix = os.urandom(8)
        header = HEADER_STRUCT.pack(MAGIC, VERSION, chunk_size, prefix)
        file_obj = open(path, "w+b")
        try:
            file_obj.write(header)
            file_obj.flush()
        except Exception:
            file_obj.close()
            path.unlink(missing_ok=True)
            raise
        return cls(Path(data_dir), relative, key, size, chunk_size, prefix, file_obj)

    @classmethod
    def open_existing(cls, data_dir: Path, meta: dict[str, Any]) -> AttachmentWriter:
        """Reopen a partial container and recover the authenticated chunk set.

        A crash or network stop can leave partially written/zero chunk slots.
        Every candidate chunk is authenticated before it is counted, so resume
        never trusts an unverified or concurrently modified slot.
        """
        size = int(meta.get("size", -1))
        if size < 0 or size > 1024 * 1024 * 1024 * 1024:
            raise VaultError("文件大小无效")
        chunk_size = int(meta.get("chunk_size", DEFAULT_CHUNK))
        if not MIN_CHUNK <= chunk_size <= MAX_CHUNK:
            raise VaultError("文件分块大小无效")
        header = _read_header(Path(data_dir), meta)
        if header.chunk_size != chunk_size:
            raise VaultError("加密附件分块参数不匹配")
        key = _key_bytes(meta)
        relative = str(meta.get("enc_path", ""))
        file_obj = None
        try:
            file_obj = open(header.path, "r+b")
        except OSError as exc:
            raise VaultError(f"无法重新打开加密附件：{exc}") from exc
        writer = cls(Path(data_dir), relative, key, size, chunk_size, header.prefix, file_obj)
        received: set[int] = set()
        received_bytes = 0
        try:
            for index in range(writer.expected_chunks):
                plain_len = _chunk_plain_len(size, chunk_size, index)
                offset = HEADER_SIZE + index * (chunk_size + TAG_SIZE)
                file_obj.seek(offset)
                ciphertext = file_obj.read(plain_len + TAG_SIZE)
                if len(ciphertext) != plain_len + TAG_SIZE:
                    continue
                try:
                    writer._aead.decrypt(
                        _attachment_nonce(header.prefix, index),
                        ciphertext,
                        writer._aad,
                    )
                except InvalidTag:
                    continue
                received.add(index)
                received_bytes += plain_len
        except OSError as exc:
            try:
                file_obj.close()
            finally:
                raise VaultError(f"扫描加密附件失败：{exc}") from exc
        with writer._lock:
            writer._received = received
            writer._received_bytes = received_bytes
        return writer

    @property
    def received_bytes(self) -> int:
        with self._lock:
            return self._received_bytes

    @property
    def received_chunks(self) -> int:
        with self._lock:
            return len(self._received)

    def write_chunk(self, index: int, data: bytes) -> None:
        index = int(index)
        if not 0 <= index < self.expected_chunks:
            raise VaultError("文件分块序号越界")
        expected = _chunk_plain_len(self.size, self.chunk_size, index)
        if len(data) != expected:
            raise VaultError("文件分块长度与文件大小不一致")
        nonce = _attachment_nonce(self.prefix, index)
        ciphertext = self._aead.encrypt(nonce, data, self._aad)
        offset = HEADER_SIZE + index * (self.chunk_size + TAG_SIZE)
        with self._lock:
            if self._finished:
                raise VaultError("附件已经结束写入")
            if index in self._received:
                raise VaultError("收到重复的文件分块")
            self._file.seek(offset)
            self._file.write(ciphertext)
            self._received.add(index)
            self._received_bytes += len(data)

    def finish(self) -> dict[str, Any]:
        with self._lock:
            if self._finished:
                raise VaultError("附件已经结束写入")
            missing = self.expected_chunks - len(self._received)
            if missing:
                raise VaultError(f"文件不完整，缺少 {missing} 个分块")
            self._file.truncate(_container_size(self.size, self.chunk_size))
            self._file.flush()
            try:
                os.fsync(self._file.fileno())
            except OSError:
                pass
            self._file.close()
            self._finished = True
        return self.metadata()

    def received_indices(self) -> set[int]:
        with self._lock:
            return set(self._received)

    def received_bitmap(self) -> bytes:
        """Return a compact little-bit-endian bitmap of authenticated chunks."""
        with self._lock:
            bitmap = bytearray((self.expected_chunks + 7) // 8)
            for index in self._received:
                bitmap[index // 8] |= 1 << (index % 8)
            return bytes(bitmap)

    def suspend(self) -> None:
        """Close the container while preserving all received chunks for resume."""
        with self._lock:
            if self._finished:
                return
            try:
                self._file.flush()
                try:
                    os.fsync(self._file.fileno())
                except OSError:
                    pass
            finally:
                self._file.close()
                self._finished = True

    def metadata(self, name: str = "") -> dict[str, Any]:
        display_name = sanitize_filename(name or self.path.name) if name else self.path.name
        return {
            "name": display_name,
            "size": self.size,
            "chunk_size": self.chunk_size,
            "chunk_count": self.expected_chunks,
            "storage": "encrypted",
            "enc_path": self.relative_path,
            "key": base64.b64encode(self.key).decode("ascii"),
        }

    def abort(self) -> None:
        try:
            with self._lock:
                if not self._finished:
                    self._file.close()
                    self._finished = True
        finally:
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                pass


@dataclass
class _ContainerHeader:
    path: Path
    chunk_size: int
    prefix: bytes


def _read_header(data_dir: Path, meta: dict[str, Any]) -> _ContainerHeader:
    relative = str(meta.get("enc_path", ""))
    if not relative:
        raise VaultError("附件缺少存储路径")
    path = (Path(data_dir) / relative).resolve()
    root = Path(data_dir).resolve()
    if root not in path.parents:
        raise VaultError("附件路径越界")
    try:
        with open(path, "rb") as handle:
            header = handle.read(HEADER_SIZE)
    except OSError as exc:
        raise VaultError(f"无法读取加密附件：{exc}") from exc
    if len(header) != HEADER_SIZE:
        raise VaultError("加密附件头部不完整")
    magic, version, chunk_size, prefix = HEADER_STRUCT.unpack(header)
    if magic != MAGIC or version != VERSION:
        raise VaultError("不支持的加密附件格式")
    if int(meta.get("chunk_size", chunk_size)) != chunk_size:
        raise VaultError("加密附件分块参数不匹配")
    return _ContainerHeader(path=path, chunk_size=chunk_size, prefix=prefix)


def iter_plaintext(data_dir: Path, meta: dict[str, Any]) -> Iterator[bytes]:
    """Yield decrypted chunks, authenticating each chunk before release."""
    size = int(meta.get("size", 0))
    if size < 0:
        raise VaultError("附件大小无效")
    header = _read_header(data_dir, meta)
    key = _key_bytes(meta)
    aead = AESGCM(key)
    aad = _aad(header.chunk_size, header.prefix)
    expected_container = _container_size(size, header.chunk_size)
    try:
        actual_size = header.path.stat().st_size
    except OSError as exc:
        raise VaultError(f"无法检查加密附件：{exc}") from exc
    if actual_size != expected_container:
        raise VaultError("加密附件尺寸不完整")
    count = _chunk_count(size, header.chunk_size)
    try:
        with open(header.path, "rb") as handle:
            for index in range(count):
                plain_len = _chunk_plain_len(size, header.chunk_size, index)
                offset = HEADER_SIZE + index * (header.chunk_size + TAG_SIZE)
                handle.seek(offset)
                ciphertext = handle.read(plain_len + TAG_SIZE)
                if len(ciphertext) != plain_len + TAG_SIZE:
                    raise VaultError("加密附件内容被截断")
                try:
                    yield aead.decrypt(_attachment_nonce(header.prefix, index), ciphertext, aad)
                except InvalidTag as exc:
                    raise VaultError("加密附件完整性校验失败") from exc
    except OSError as exc:
        raise VaultError(f"读取加密附件失败：{exc}") from exc


def decrypt_to_file(data_dir: Path, meta: dict[str, Any], destination: Path) -> Path:
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = destination.with_name(f".{destination.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(temp_path, "wb") as handle:
            handle.writelines(iter_plaintext(data_dir, meta))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, destination)
        return destination
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def decrypt_to_temp(data_dir: Path, meta: dict[str, Any]) -> Path:
    name = sanitize_filename(str(meta.get("name", "attachment")))
    stem, suffix = os.path.splitext(name)
    stem = sanitize_filename(stem or "V6Link", fallback="V6Link")[:40]
    suffix = suffix[:20]
    fd, raw_path = tempfile.mkstemp(prefix=f"V6Link-{stem}-", suffix=suffix)
    os.close(fd)
    path = Path(raw_path)
    try:
        destination = decrypt_to_file(data_dir, meta, path)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    with _TEMP_LOCK:
        _TEMP_FILES.add(destination)
    return destination


def cleanup_temp_file(path: Path) -> None:
    with _TEMP_LOCK:
        _TEMP_FILES.discard(Path(path))
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass
