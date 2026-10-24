"""Attachment export, open and automatic download helpers."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from ..errors import (
    TransferError,
)
from ..netutil import (
    sanitize_filename,
)

from .models import (
    log,
    _now,
    _friendly_error,
)


class _ExportMixin:
    """Attachment export, open and automatic download helpers."""

    @staticmethod
    def _unique_download_path(folder: Path, name: str) -> Path:
        safe_name = sanitize_filename(name or "download.bin", fallback="download.bin")
        candidate = folder / safe_name
        if not candidate.exists():
            return candidate
        stem = Path(safe_name).stem or "download"
        suffix = Path(safe_name).suffix
        for index in range(1, 10000):
            candidate = folder / f"{stem} ({index}){suffix}"
            if not candidate.exists():
                return candidate
        raise TransferError("下载目录中同名文件过多，无法生成新文件名")

    async def _auto_export_incoming(self, message_id: str, file_meta: dict[str, Any]) -> None:
        from ..filestore import decrypt_to_file

        name = str(file_meta.get("name", "download.bin"))
        try:
            async with self._download_lock:
                folder = self._downloads_dir
                await asyncio.to_thread(folder.mkdir, parents=True, exist_ok=True)
                target = await asyncio.to_thread(self._unique_download_path, folder, name)
                path = await asyncio.to_thread(
                    decrypt_to_file,
                    self.vault.data_dir,
                    file_meta,
                    target,
                )
                updated = self.vault.update_message(
                    message_id,
                    file_update={
                        "export_path": str(path),
                        "auto_downloaded": True,
                        "exported_at": _now(),
                    },
                )
                self._emit("message_updated", message=updated)
                self._emit(
                    "file_auto_saved",
                    message_id=message_id,
                    contact_id=updated.get("contact_id", ""),
                    name=name,
                    path=str(path),
                )
                log.info("incoming file auto-saved to downloads: %s", path.name)
        except Exception as exc:
            reason = _friendly_error(exc)
            log.warning("auto-save incoming file failed: %s", reason)
            self._emit(
                "toast",
                message=f"文件已接收，但自动保存到下载目录失败：{reason}。可在文件卡片上选择「另存为」。",
                level="warning",
            )

    async def export_attachment(self, message_id: str, destination: str | Path) -> Path:
        from ..filestore import decrypt_to_file

        message = self.vault.get_message(message_id)
        if message is None or message.get("kind") != "file":
            raise ValueError("找不到该文件消息")
        file_meta = message.get("file") or {}
        if message.get("direction") != "in" or file_meta.get("storage") != "encrypted":
            raise ValueError("该文件没有加密存储副本")
        target = Path(destination).expanduser()
        path = await asyncio.to_thread(
            decrypt_to_file,
            self.vault.data_dir,
            file_meta,
            target,
        )
        try:
            updated = self.vault.update_message(
                message_id,
                file_update={
                    "export_path": str(path),
                    "exported_at": _now(),
                },
            )
            self._emit("message_updated", message=updated)
        except Exception:
            log.debug("failed to remember exported file path", exc_info=True)
        self._emit("attachment_open", message_id=message_id, path=str(path), opened=False)
        return path

    async def open_attachment(self, message_id: str) -> Path:
        from ..filestore import decrypt_to_temp

        message = self.vault.get_message(message_id)
        if message is None or message.get("kind") != "file":
            raise ValueError("找不到该文件消息")
        file_meta = message.get("file") or {}
        if message.get("direction") != "in" or file_meta.get("storage") != "encrypted":
            raise ValueError("该文件没有加密存储副本")
        path = await asyncio.to_thread(
            decrypt_to_temp,
            self.vault.data_dir,
            file_meta,
        )
        self._emit("attachment_open", message_id=message_id, path=str(path), opened=True)
        return path
