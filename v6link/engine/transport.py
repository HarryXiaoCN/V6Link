"""Transport abstraction: TCP/IPv6 socket setup, listening and connecting."""

from __future__ import annotations

import asyncio
import contextlib
import socket
import sys
from collections.abc import Awaitable, Callable
from typing import Protocol

from .models import CONNECT_TIMEOUT

StreamHandler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


class Transport(Protocol):
    """The byte-stream operations the connection manager needs."""

    async def listen(
        self,
        host: str,
        port: int,
        handler: StreamHandler,
    ) -> asyncio.AbstractServer: ...

    async def open(
        self,
        host: str,
        port: int,
        timeout: float = CONNECT_TIMEOUT,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]: ...

    async def close_server(self, server: asyncio.AbstractServer) -> None: ...


class TcpTransport:
    """IPv6 TCP transport with the platform socket options V6Link needs."""

    def __init__(self, family: int = socket.AF_INET6):
        self.family = family

    @staticmethod
    def configure_socket(writer: asyncio.StreamWriter) -> None:
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

    async def open(
        self,
        host: str,
        port: int,
        timeout: float = CONNECT_TIMEOUT,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, family=self.family),
            timeout=timeout,
        )
        self.configure_socket(writer)
        return reader, writer

    async def listen(
        self,
        host: str,
        port: int,
        handler: StreamHandler,
    ) -> asyncio.AbstractServer:
        raw_socket = socket.socket(self.family, socket.SOCK_STREAM)
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            # On Windows SO_REUSEADDR would let another local process bind the
            # same port; exclusive use makes port conflicts fail closed.
            raw_socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            raw_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "IPV6_V6ONLY"):
            try:
                raw_socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            except OSError:
                pass
        raw_socket.setblocking(False)
        try:
            raw_socket.bind((host, int(port)))
            raw_socket.listen(128)
        except Exception:
            raw_socket.close()
            raise
        try:
            return await asyncio.start_server(handler, sock=raw_socket, backlog=128)
        except Exception:
            raw_socket.close()
            raise

    async def close_server(self, server: asyncio.AbstractServer) -> None:
        server.close()
        with contextlib.suppress(Exception):
            await server.wait_closed()