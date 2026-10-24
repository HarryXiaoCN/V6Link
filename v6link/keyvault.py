"""Protection of the random master key that encrypts the local vault.

On Windows the key is wrapped with DPAPI (current-user scope), so the raw
master key is never stored on disk. On other platforms the key file is created
with owner-only permissions (0600), which is the strongest portable fallback
available without introducing an external keyring dependency.
"""

from __future__ import annotations

import ctypes
import os
import sys
import tempfile
from pathlib import Path

from .errors import VaultError

KEY_MAGIC = b"V6KEY01"
METHOD_DPAPI = 1
METHOD_RAW = 2
KEY_BYTES = 32
DPAPI_ENTROPY = b"V6Link/local-vault-key/v1"


def default_data_dir() -> Path:
    override = os.environ.get("V6LINK_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(root) / "V6Link"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "V6Link"
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "v6link"


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temp_path, mode)
        except OSError:
            pass
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.c_uint32),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _dpapi_protect(data: bytes) -> bytes:
    if sys.platform != "win32":
        raise VaultError("DPAPI 仅适用于 Windows")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    in_buffer = ctypes.create_string_buffer(data, len(data))
    entropy_buffer = ctypes.create_string_buffer(DPAPI_ENTROPY, len(DPAPI_ENTROPY))
    in_blob = _DataBlob(len(data), ctypes.cast(in_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    entropy_blob = _DataBlob(
        len(DPAPI_ENTROPY),
        ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_ubyte)),
    )
    out_blob = _DataBlob()
    ok = crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        "V6Link vault key",
        ctypes.byref(entropy_blob),
        None,
        None,
        0x1,  # CRYPTPROTECT_UI_FORBIDDEN
        ctypes.byref(out_blob),
    )
    if not ok:
        raise VaultError("无法使用 Windows DPAPI 保护本地密钥")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _dpapi_unprotect(data: bytes) -> bytes:
    if sys.platform != "win32":
        raise VaultError("此密钥由 Windows DPAPI 保护，需要在原来的 Windows 用户下打开")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    in_buffer = ctypes.create_string_buffer(data, len(data))
    entropy_buffer = ctypes.create_string_buffer(DPAPI_ENTROPY, len(DPAPI_ENTROPY))
    in_blob = _DataBlob(len(data), ctypes.cast(in_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    entropy_blob = _DataBlob(
        len(DPAPI_ENTROPY),
        ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_ubyte)),
    )
    out_blob = _DataBlob()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        ctypes.byref(entropy_blob),
        None,
        None,
        0x1,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise VaultError("无法解密本地密钥：请使用创建该数据的 Windows 用户登录")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


class MasterKeyStore:
    """Loads or creates the 256-bit key protecting the encrypted vault."""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.key_path = self.data_dir / "vault.key"

    def load_or_create(self) -> bytes:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if self.key_path.exists():
            return self._read()
        key = os.urandom(KEY_BYTES)
        if sys.platform == "win32":
            wrapped = _dpapi_protect(key)
            method = METHOD_DPAPI
        else:
            wrapped = key
            method = METHOD_RAW
        payload = KEY_MAGIC + bytes([method]) + wrapped
        atomic_write(self.key_path, payload, mode=0o600)
        return key

    def _read(self) -> bytes:
        try:
            raw = self.key_path.read_bytes()
        except OSError as exc:
            raise VaultError(f"无法读取本地密钥：{exc}") from exc
        if len(raw) < len(KEY_MAGIC) + 2 or not raw.startswith(KEY_MAGIC):
            raise VaultError("本地密钥文件格式无效")
        method = raw[len(KEY_MAGIC)]
        wrapped = raw[len(KEY_MAGIC) + 1 :]
        if method == METHOD_DPAPI:
            key = _dpapi_unprotect(wrapped)
        elif method == METHOD_RAW:
            key = wrapped
        else:
            raise VaultError("不支持的本地密钥保护方式")
        if len(key) != KEY_BYTES:
            raise VaultError("本地主密钥长度无效")
        return key
