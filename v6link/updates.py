"""Peer-to-peer application updates.

Design
------
* A newer instance offers *its own* program payload — the running executable
  when frozen, or a reproducible source archive when running from a checkout —
  to an older, already-approved contact. The bytes travel over the same
  authenticated AES-256-GCM channel as chat messages and files.
* Every offer is signed with the sender's long-term Ed25519 identity key. The
  receiver verifies that signature against the pinned contact identity, so a
  payload cannot be swapped for a different program between the offer and the
  bytes that finally land in ``updates/pending``.
* Installing always requires two explicit user decisions (accept the offer,
  then confirm the restart). Immediately before the swap the staged file is
  hashed again, the previous executable is kept as a backup, and a generated
  PowerShell script performs the swap only after this process has exited.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric import ed25519

from . import __version__
from .errors import UpdateError

SIGN_CONTEXT = b"V6Link/update/v1\x00"
SIGNED_FIELDS = ("offer_id", "version", "size", "sha256", "kind", "filename", "notes")
UPDATE_CHUNK = 256 * 1024
MIN_UPDATE_BYTES = 4096
MAX_UPDATE_BYTES = 512 * 1024 * 1024
MAX_NOTES = 500
KINDS = ("exe", "source")
STALE_BACKUP_SECONDS = 7 * 24 * 3600

_VERSION_RE = re.compile(r"^\d{1,4}(?:\.\d{1,4}){0,3}$")
_OFFER_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,120}$")

_SOURCE_INCLUDE = (
    "main.py",
    "run.bat",
    "V6Link.spec",
    "pyproject.toml",
    "requirements.txt",
    "requirements-build.txt",
    "README.md",
    "SECURITY.md",
)
_SOURCE_DIRS = ("v6link", "assets", "packaging")
_SOURCE_SKIP_PARTS = {"__pycache__", ".ruff_cache", ".pytest_cache", ".git"}


# --------------------------------------------------------------------- version


def version_key(value: Any) -> tuple[int, ...]:
    """Turn ``0.10.2`` into ``(0, 10, 2)``; unparsable text sorts as zero."""
    parts = re.findall(r"\d+", str(value or ""))
    if not parts:
        return (0,)
    return tuple(int(part) for part in parts[:4])


def compare_versions(left: Any, right: Any) -> int:
    """Return -1, 0 or 1 comparing dotted version strings numerically."""
    a = version_key(left)
    b = version_key(right)
    width = max(len(a), len(b))
    a = a + (0,) * (width - len(a))
    b = b + (0,) * (width - len(b))
    return (a > b) - (a < b)


def is_newer(candidate: Any, current: Any) -> bool:
    return compare_versions(candidate, current) > 0


def normalize_version(value: Any) -> str:
    text = str(value or "").strip()
    if not _VERSION_RE.match(text):
        raise UpdateError("更新版本号无效")
    return text


def current_version() -> str:
    return __version__


# ----------------------------------------------------------------- payload side


def is_frozen() -> bool:
    """True when the process is a PyInstaller executable."""
    return bool(getattr(sys, "frozen", False))


def _persisted_executable() -> Path | None:
    """Return the executable file a user actually launched, even in one-file mode.

    A PyInstaller one-file build runs the Python code in a child process whose
    ``sys.executable`` lives inside the temporary ``_MEIxxxx`` extraction
    directory. Replacing that file would be pointless (it is deleted on exit),
    so the bootloader parent process is asked for the real path instead.
    """
    try:
        exe = Path(sys.executable).resolve()
    except OSError:
        return None
    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass:
        return exe
    try:
        extraction_dir = Path(meipass).resolve()
    except OSError:
        return exe
    if exe.parent != extraction_dir:
        return exe
    with contextlib.suppress(Exception):
        import psutil

        parent = psutil.Process().parent()
        if parent is not None:
            candidate = Path(parent.exe()).resolve()
            if candidate.is_file() and candidate != exe:
                return candidate
    return None


def running_executable() -> Path | None:
    if not is_frozen():
        return None
    candidate = _persisted_executable()
    if candidate is None or not candidate.is_file():
        return None
    return candidate


def source_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _source_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for name in _SOURCE_INCLUDE:
        candidate = root / name
        if candidate.is_file():
            files.append(candidate)
    for name in _SOURCE_DIRS:
        base = root / name
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            if _SOURCE_SKIP_PARTS.intersection(path.parts):
                continue
            if path.suffix in {".pyc", ".pyo"}:
                continue
            files.append(path)
    return files


def _newest_mtime(files: list[Path]) -> float:
    newest = 0.0
    for path in files:
        with contextlib.suppress(OSError):
            newest = max(newest, path.stat().st_mtime)
    return newest


def source_archive(data_dir: Path, *, version: str | None = None) -> Path:
    """Build (or reuse) a zip of the checkout so source runs can share updates."""
    root = source_root()
    files = _source_files(root)
    if not files:
        raise UpdateError("找不到可打包的源码文件")
    label = version or __version__
    target = updates_root(data_dir) / "outgoing" / f"V6Link-{label}-source.zip"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and target.stat().st_mtime >= _newest_mtime(files):
        return target
    temporary = target.with_name(f".{target.name}.tmp")
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in files:
                archive.write(path, path.relative_to(root).as_posix())
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            with contextlib.suppress(OSError):
                temporary.unlink()
    return target


def payload_for_offer(
    data_dir: Path,
    *,
    version: str | None = None,
) -> tuple[Path, str, str]:
    """Return ``(path, kind, filename)`` for the program this process runs."""
    label = version or __version__
    exe = running_executable()
    if exe is not None and exe.is_file():
        return exe, "exe", f"V6Link-{label}.exe"
    archive = source_archive(data_dir, version=label)
    return archive, "source", archive.name


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_payload(path: Path) -> tuple[int, str]:
    """Return ``(size, sha256)`` while checking the payload is installable."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise UpdateError(f"无法读取更新包：{exc}") from exc
    if not MIN_UPDATE_BYTES <= size <= MAX_UPDATE_BYTES:
        raise UpdateError("更新包大小超出允许范围")
    return size, sha256_file(path)


# ------------------------------------------------------------------- signature


def _clean_notes(value: Any) -> str:
    text = "".join(
        ch if ch.isprintable() and ch not in "\x00\t\r" else " " for ch in str(value or "")
    )
    return " ".join(text.split())[:MAX_NOTES]


def _signing_payload(fields: dict[str, Any]) -> bytes:
    parts: list[bytes] = []
    for name in SIGNED_FIELDS:
        value = fields.get(name)
        if name == "size":
            value = int(value or 0)
        parts.append(str(value).encode("utf-8"))
    return SIGN_CONTEXT + b"\x00".join(parts)


def normalize_manifest(raw: Any) -> dict[str, Any]:
    """Validate an offer received from the network and drop unknown fields."""
    if not isinstance(raw, dict):
        raise UpdateError("更新邀请格式无效")
    offer_id = str(raw.get("offer_id") or "").strip().lower()
    if not _OFFER_ID_RE.match(offer_id):
        raise UpdateError("更新邀请标识无效")
    version = normalize_version(raw.get("version"))
    try:
        size = int(raw.get("size") or 0)
    except (TypeError, ValueError) as exc:
        raise UpdateError("更新包大小无效") from exc
    if not MIN_UPDATE_BYTES <= size <= MAX_UPDATE_BYTES:
        raise UpdateError("更新包大小超出允许范围")
    sha = str(raw.get("sha256") or "").strip().lower()
    if not _SHA256_RE.match(sha):
        raise UpdateError("更新包校验值无效")
    kind = str(raw.get("kind") or "").strip().lower()
    if kind not in KINDS:
        raise UpdateError("更新包类型不受支持")
    filename = str(raw.get("filename") or "").strip()
    if not _FILENAME_RE.match(filename):
        raise UpdateError("更新包文件名无效")
    signature = str(raw.get("signature") or "").strip()
    if not signature or len(signature) > 128:
        raise UpdateError("更新包签名无效")
    return {
        "offer_id": offer_id,
        "version": version,
        "size": size,
        "sha256": sha,
        "kind": kind,
        "filename": filename,
        "notes": _clean_notes(raw.get("notes")),
        "signature": signature,
    }


def build_manifest(
    identity: Any,
    *,
    path: Path,
    kind: str,
    filename: str,
    notes: str = "",
    version: str | None = None,
) -> dict[str, Any]:
    """Sign a local payload so the receiver can attribute it to this identity."""
    if not _FILENAME_RE.match(filename):
        raise UpdateError("更新包文件名无效")
    if kind not in KINDS:
        raise UpdateError("更新包类型不受支持")
    size, sha = inspect_payload(Path(path))
    fields = {
        "offer_id": os.urandom(16).hex(),
        "version": normalize_version(version or __version__),
        "size": size,
        "sha256": sha,
        "kind": kind,
        "filename": filename,
        "notes": _clean_notes(notes),
    }
    signature = identity.sign(_signing_payload(fields))
    return {
        **fields,
        "signature": base64.b64encode(signature).decode("ascii"),
        "created_at": time.time(),
    }


def verify_manifest(manifest: dict[str, Any], peer_public_bytes: bytes) -> None:
    """Prove the offer was signed by the pinned contact identity."""
    cleaned = normalize_manifest(manifest)
    try:
        signature = base64.b64decode(cleaned["signature"].encode("ascii"), validate=True)
    except Exception as exc:
        raise UpdateError("更新包签名格式无效") from exc
    if len(signature) != 64:
        raise UpdateError("更新包签名长度无效")
    try:
        key = ed25519.Ed25519PublicKey.from_public_bytes(peer_public_bytes)
    except ValueError as exc:
        raise UpdateError("更新包签名密钥无效") from exc
    try:
        key.verify(signature, _signing_payload(cleaned))
    except InvalidSignature as exc:
        raise UpdateError("更新包签名验证失败，内容可能已被篡改") from exc


# -------------------------------------------------------------------- staging


def updates_root(data_dir: Path) -> Path:
    return Path(data_dir) / "updates"


def incoming_dir(data_dir: Path) -> Path:
    return updates_root(data_dir) / "incoming"


def pending_dir(data_dir: Path) -> Path:
    return updates_root(data_dir) / "pending"


def ensure_dirs(data_dir: Path) -> None:
    incoming_dir(data_dir).mkdir(parents=True, exist_ok=True)
    pending_dir(data_dir).mkdir(parents=True, exist_ok=True)


def part_path(data_dir: Path, offer_id: str) -> Path:
    if not _OFFER_ID_RE.match(offer_id):
        raise UpdateError("更新邀请标识无效")
    return incoming_dir(data_dir) / f"{offer_id}.part"


def staged_path(data_dir: Path, filename: str) -> Path:
    if not _FILENAME_RE.match(filename):
        raise UpdateError("更新包文件名无效")
    return pending_dir(data_dir) / filename


def discard_part(data_dir: Path, offer_id: str) -> None:
    with contextlib.suppress(OSError):
        part_path(data_dir, offer_id).unlink(missing_ok=True)


# -------------------------------------------------------------------- install


@dataclass(frozen=True)
class InstallPlan:
    target: Path
    staged: Path
    backup: Path
    script: Path


def _ps_literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _render_script(target: Path, staged: Path, backup: Path, process_id: int) -> str:
    lines = [
        "# Generated by V6Link. Waits for the running instance, swaps the",
        "# executable, restarts it, and restores the backup on failure.",
        "$ErrorActionPreference = 'Stop'",
        f"$oldProcessId = {int(process_id)}",
        f"$target = {_ps_literal(str(target))}",
        f"$staged = {_ps_literal(str(staged))}",
        f"$backup = {_ps_literal(str(backup))}",
        "for ($i = 0; $i -lt 120; $i++) {",
        "    if (-not (Get-Process -Id $oldProcessId -ErrorAction SilentlyContinue)) { break }",
        "    Start-Sleep -Milliseconds 500",
        "}",
        "try {",
        "    Move-Item -LiteralPath $target -Destination $backup -Force",
        "} catch { }",
        "if (-not (Test-Path -LiteralPath $backup)) {",
        "    Start-Process -FilePath $target",
        "    exit 1",
        "}",
        "try {",
        "    Copy-Item -LiteralPath $staged -Destination $target -Force",
        "} catch {",
        "    Copy-Item -LiteralPath $backup -Destination $target -Force",
        "    Start-Process -FilePath $target",
        "    exit 1",
        "}",
        "Start-Process -FilePath $target",
        "Remove-Item -LiteralPath $staged -Force -ErrorAction SilentlyContinue",
        "Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue",
        "exit 0",
        "",
    ]
    return "\r\n".join(lines)


def prepare_install(
    data_dir: Path,
    staged: str | Path,
    *,
    version: str,
    sha256: str,
    size: int,
) -> InstallPlan:
    """Re-verify a downloaded payload and generate the swap script."""
    if sys.platform != "win32":
        raise UpdateError("自动安装目前只支持 Windows；更新包已保存到本地")
    target = running_executable()
    if target is None or not target.is_file():
        raise UpdateError("当前以源码方式运行，无法自动替换程序；更新包已保存到本地")
    staged_file = Path(staged).resolve()
    pending = pending_dir(data_dir).resolve()
    if staged_file.parent != pending:
        raise UpdateError("更新包位置异常，已阻止安装")
    if not staged_file.is_file():
        raise UpdateError("更新包已不存在，请重新接收")
    if staged_file.stat().st_size != int(size):
        raise UpdateError("更新包大小与邀请不一致，已阻止安装")
    if sha256_file(staged_file) != str(sha256).lower():
        raise UpdateError("更新包校验失败，已阻止安装")
    with open(staged_file, "rb") as handle:
        if handle.read(2) != b"MZ":
            raise UpdateError("更新包不是有效的 Windows 程序，已阻止安装")
    ensure_dirs(data_dir)
    backup = target.with_name(f"{target.name}.old-{normalize_version(__version__)}")
    candidate = backup
    counter = 1
    while candidate.exists():
        candidate = backup.with_name(f"{backup.name}.{counter}")
        counter += 1
    backup = candidate
    script = updates_root(data_dir) / "apply-update.ps1"
    script.write_text(
        _render_script(target, staged_file, backup, os.getpid()),
        encoding="utf-8-sig",
        newline="",
    )
    return InstallPlan(
        target=target,
        staged=staged_file,
        backup=backup,
        script=script,
    )


def _powershell_path() -> Path:
    system_root = os.environ.get("SYSTEMROOT") or r"C:\Windows"
    candidate = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if candidate.is_file():
        return candidate
    found = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    if found:
        return Path(found)
    raise UpdateError("找不到 PowerShell，无法自动完成更新")


def launch_install(plan: InstallPlan) -> None:
    """Start the detached swap script; the caller must quit immediately after.

    ``DETACHED_PROCESS`` cannot be used here: Windows PowerShell 5.1 exits
    immediately without running the script when it has no console at all, so
    the updater gets its own hidden console and process group instead.
    """
    powershell = _powershell_path()
    flags = 0
    if sys.platform == "win32":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    subprocess.Popen(
        [
            str(powershell),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-WindowStyle",
            "Hidden",
            "-File",
            str(plan.script),
        ],
        creationflags=flags,
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


# -------------------------------------------------------------------- cleanup


def cleanup_on_start(data_dir: Path) -> None:
    """Remove stale downloads and backups left behind by earlier updates."""
    root = updates_root(data_dir)
    if not root.is_dir():
        return
    now = time.time()
    for part in list(incoming_dir(data_dir).glob("*.part")):
        with contextlib.suppress(OSError):
            if now - part.stat().st_mtime > 3600:
                part.unlink()
    with contextlib.suppress(OSError):
        (root / "apply-update.ps1").unlink(missing_ok=True)
    exe = running_executable()
    if exe is None:
        return
    for stale in exe.parent.glob(f"{exe.name}.old-*"):
        with contextlib.suppress(OSError):
            if now - stale.stat().st_mtime > STALE_BACKUP_SECONDS:
                stale.unlink()
