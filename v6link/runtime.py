"""Background asyncio runtime so cryptography and disk I/O never block Qt."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import errno
import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .engine import ChatEngine
from .errors import V6LinkError
from .updates import InstallPlan, launch_install, prepare_install
from .vault import Vault

log = logging.getLogger("v6link.runtime")
EventSink = Callable[[dict[str, Any]], None]


class BackendRuntime:
    """Owns the network engine thread and its own event loop."""

    def __init__(
        self,
        vault: Vault,
        event_sink: EventSink,
        *,
        bind_host: str = "::",
        listen_port: int | None = None,
    ):
        self.vault = vault
        self._event_sink = event_sink
        self._bind_host = bind_host
        self._listen_port = listen_port
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._engine: ChatEngine | None = None
        self._ready = threading.Event()
        self._startup_error: BaseException | None = None
        self._stopping = False
        self._event_muted = False

    def start(self, timeout: float = 15.0) -> None:
        if self._thread is not None:
            raise RuntimeError("后端已经启动")
        self._thread = threading.Thread(target=self._thread_main, name="v6link-backend", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            raise TimeoutError("后端启动超时")
        if self._startup_error is not None:
            raise RuntimeError(str(self._startup_error)) from self._startup_error

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            self._engine = ChatEngine(
                self.vault,
                self._thread_event,
                bind_host=self._bind_host,
                listen_port=self._listen_port,
            )
            try:
                loop.run_until_complete(self._engine.start())
            except Exception as exc:
                log.exception("listener failed to start")
                address_in_use = isinstance(exc, OSError) and (
                    getattr(exc, "winerror", None) == 10048 or exc.errno == errno.EADDRINUSE
                )
                if address_in_use:
                    friendly = (
                        f"IPv6 监听失败：端口 {self._engine.port if self._engine else ''} "
                        "已被其他程序占用，请在设置中更换监听端口。"
                    )
                else:
                    friendly = f"IPv6 监听启动失败：{_exc_text(exc)}"
                self._thread_event(
                    {"type": "error", "scope": "network", "message": friendly}
                )
                self._thread_event(
                    {
                        "type": "network",
                        "listening": False,
                        "port": self._engine.port if self._engine else 0,
                        "host": self._bind_host,
                    }
                )
            self._ready.set()
            loop.run_forever()
        except BaseException as exc:  # startup failure before ready
            self._startup_error = exc
            self._ready.set()
        finally:
            try:
                pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                log.debug("event loop shutdown issue", exc_info=True)
            loop.close()
            self._loop = None

    def _thread_event(self, payload: dict[str, Any]) -> None:
        if self._event_muted:
            return
        try:
            self._event_sink(payload)
        except Exception:
            log.debug("UI event sink failed", exc_info=True)

    def _invoke(self, method_name: str, *args: Any, scope: str = "operation") -> concurrent.futures.Future:
        engine = self._engine
        loop = self._loop
        if engine is None or loop is None or not loop.is_running():
            raise RuntimeError("后端尚未运行")
        coroutine = getattr(engine, method_name)(*args)
        future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        future.add_done_callback(lambda fut: self._report_future_error(fut, scope))
        return future

    def _report_future_error(self, future: concurrent.futures.Future, scope: str) -> None:
        try:
            future.result()
        except concurrent.futures.CancelledError:
            return
        except BaseException as exc:
            self._thread_event(
                {
                    "type": "error",
                    "scope": scope,
                    "message": _exc_text(exc),
                }
            )

    # ------------------------------------------------------------- public APIs

    def send_text(self, contact_id: str, text: str) -> concurrent.futures.Future:
        return self._invoke("send_text", contact_id, text, scope="send")

    def send_file(self, contact_id: str, path: str | Path) -> concurrent.futures.Future:
        return self._invoke("send_file", contact_id, str(path), scope="send")

    def retry_message(self, message_id: str) -> concurrent.futures.Future:
        return self._invoke("retry_message", message_id, scope="send")

    def accept_file_offer(self, file_id: str) -> concurrent.futures.Future:
        return self._invoke("respond_file_offer", file_id, True, scope="file")

    def reject_file_offer(self, file_id: str) -> concurrent.futures.Future:
        return self._invoke("respond_file_offer", file_id, False, scope="file")

    def cancel_transfer(self, message_id: str) -> concurrent.futures.Future:
        return self._invoke("cancel_transfer", message_id, scope="file")

    def add_contact(self, name: str, peer_id: str = "", addresses: list[dict[str, Any]] | None = None) -> concurrent.futures.Future:
        return self._invoke("add_contact", name, peer_id, addresses or [], scope="contact")

    def update_contact(self, contact_id: str, changes: dict[str, Any]) -> concurrent.futures.Future:
        return self._invoke("update_contact", contact_id, changes, scope="contact")

    def delete_contact(self, contact_id: str) -> concurrent.futures.Future:
        return self._invoke("delete_contact", contact_id, scope="contact")

    def accept_pending_request(self, request_id: str) -> concurrent.futures.Future:
        return self._invoke("accept_pending_request", request_id, scope="contact")

    def decline_pending_request(self, request_id: str) -> concurrent.futures.Future:
        return self._invoke("decline_pending_request", request_id, scope="contact")

    def apply_settings(
        self,
        display_name: str,
        listen_port: int,
        parallel_streams: int,
        downloads_dir: str | Path | None = None,
    ) -> concurrent.futures.Future:
        return self._invoke(
            "apply_settings",
            display_name,
            listen_port,
            parallel_streams,
            downloads_dir,
            scope="settings",
        )

    def open_attachment(self, message_id: str) -> concurrent.futures.Future:
        return self._invoke("open_attachment", message_id, scope="attachment")

    def push_update(self, contact_id: str, notes: str = "") -> concurrent.futures.Future:
        return self._invoke("push_update", contact_id, notes, scope="update")

    def respond_update_offer(self, offer_id: str, accepted: bool) -> concurrent.futures.Future:
        return self._invoke("respond_update_offer", offer_id, accepted, scope="update")

    def cancel_update(self, offer_id: str) -> concurrent.futures.Future:
        return self._invoke("cancel_update", offer_id, scope="update")

    def install_update(
        self,
        path: str | Path,
        *,
        version: str,
        sha256: str,
        size: int,
    ) -> InstallPlan:
        """Verify a staged payload and hand the swap to a detached script.

        Runs on the caller's thread: this is local disk work that must finish
        before the GUI exits, and it never touches the engine event loop.
        """
        plan = prepare_install(
            self.vault.data_dir,
            path,
            version=version,
            sha256=sha256,
            size=size,
        )
        launch_install(plan)
        return plan

    def export_attachment(self, message_id: str, destination: str | Path) -> concurrent.futures.Future:
        return self._invoke("export_attachment", message_id, str(destination), scope="attachment")

    def snapshot(self) -> dict[str, Any]:
        engine = self._engine
        if engine is not None:
            with contextlib.suppress(Exception):
                return engine.snapshot()
        # During startup or failure, build the static snapshot directly.
        return {
            "profile": self.vault.profile(),
            "settings": self.vault.settings(),
            "peer_id": self.vault.my_peer_id,
            "contacts": self.vault.list_contacts(),
            "pending_requests": self.vault.list_pending_requests(),
            "network": {"listening": False, "port": 0, "host": self._bind_host},
        }

    @property
    def listen_port(self) -> int:
        return self._engine.port if self._engine is not None else int(self._listen_port or 0)

    def stop(self, timeout: float = 10.0) -> None:
        if self._stopping:
            return
        self._stopping = True
        loop = self._loop
        engine = self._engine
        if loop is not None and loop.is_running():
            if engine is not None:
                try:
                    future = asyncio.run_coroutine_threadsafe(engine.stop(), loop)
                    future.result(timeout=timeout)
                except Exception:
                    log.debug("engine stop failed", exc_info=True)
            loop.call_soon_threadsafe(loop.stop)
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._engine = None
        self._thread = None
        self._loop = None

    def mute_events(self) -> None:
        self._event_muted = True


def _exc_text(exc: BaseException) -> str:
    if isinstance(exc, V6LinkError):
        return str(exc)
    if isinstance(exc, concurrent.futures.TimeoutError):
        return "操作超时"
    text = str(exc).strip()
    return text or exc.__class__.__name__


