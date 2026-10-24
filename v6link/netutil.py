"""IPv6 endpoint helpers, local address discovery, and text formatting."""

from __future__ import annotations

import ipaddress
import os
import re
import socket
import time
from dataclasses import dataclass

try:  # Optional, but bundled in requirements for complete interface discovery.
    import psutil
except Exception:  # pragma: no cover - exercised on minimal installations
    psutil = None


@dataclass(frozen=True)
class LocalAddress:
    host: str
    interface: str = ""
    kind: str = "global"

    def endpoint(self, port: int) -> str:
        return format_endpoint(self.host, port)


def split_zone(value: str) -> tuple[str, str]:
    """Split ``fe80::1%ethernet`` into address and zone."""
    value = value.strip()
    if "%" in value:
        base, zone = value.split("%", 1)
        return base, zone
    return value, ""


def normalize_ipv6_host(raw: str) -> str:
    """Validate and compress an IPv6 literal, preserving an optional zone."""
    if not isinstance(raw, str):
        raise ValueError("IPv6 地址必须是文本")
    value = raw.strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    base, zone = split_zone(value)
    try:
        parsed = ipaddress.IPv6Address(base)
    except ValueError as exc:
        raise ValueError("不是有效的 IPv6 地址") from exc
    if parsed.is_unspecified:
        raise ValueError("不能使用未指定地址 ::")
    text = parsed.compressed
    return f"{text}%{zone}" if zone else text


def parse_endpoint(raw: str, default_port: int = 4455) -> tuple[str, int]:
    """Parse an IPv6 endpoint.

    Supported forms::

       240e:abcd::1
       [240e:abcd::1]:4455
       [fe80::1%ethernet]:4455
       fe80::1%12

    A bare IPv6 address uses ``default_port``. An IPv6 address with a port
    must use brackets because an unbracketed final ``:4455`` is ambiguous.
    """
    if not isinstance(raw, str):
        raise ValueError("地址必须是文本")
    value = raw.strip()
    port = int(default_port)
    if not value:
        raise ValueError("地址不能为空")
    if value.startswith("["):
        close = value.find("]")
        if close < 0:
            raise ValueError("IPv6 地址缺少右方括号")
        host = value[1:close]
        tail = value[close + 1 :]
        if tail:
            if not tail.startswith(":"):
                raise ValueError("方括号后只能是端口号")
            port_text = tail[1:]
            if not port_text.isdigit():
                raise ValueError("端口必须是数字")
            port = int(port_text)
    else:
        host = value
        if host.count(":") == 1 and host.rsplit(":", 1)[-1].isdigit():
            # Avoid silently treating part of an IPv6 address as a port. A
            # single colon can only be an IPv4/hostname:port form, which this
            # IPv6-only app intentionally rejects.
            raise ValueError("IPv6 地址包含端口时请使用 [地址]:端口")
    normalize_ipv6_host(host)
    if not 1 <= int(port) <= 65535:
        raise ValueError("端口必须在 1–65535 之间")
    return host.strip(), int(port)


def format_endpoint(host: str, port: int) -> str:
    return f"[{host}]:{int(port)}"


def short_peer_id(peer_id: str) -> str:
    if not peer_id:
        return "未知身份"
    compact = re.sub(r"[^0-9a-fA-F]", "", peer_id)
    if len(compact) >= 16:
        return " ".join(compact[i : i + 4] for i in range(0, 16, 4))
    return peer_id[:16]


def fingerprint_groups(peer_id: str) -> str:
    compact = re.sub(r"[^0-9a-fA-F]", "", peer_id)
    if not compact:
        return "—"
    return " ".join(compact[i : i + 4] for i in range(0, len(compact), 4))


def human_size(num_bytes: float) -> str:
    num = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(num)} {unit}"
            return f"{num:.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} TB"


def human_speed(bytes_per_second: float) -> str:
    if not bytes_per_second or bytes_per_second < 1:
        return "—"
    return f"{human_size(bytes_per_second)}/s"


def elapsed_label(timestamp: float | None) -> str:
    if not timestamp:
        return "尚未直连"
    delta = max(0, time.time() - timestamp)
    if delta < 60:
        return "刚刚在线"
    if delta < 3600:
        return f"{int(delta // 60)} 分钟前在线"
    if delta < 86400:
        return f"{int(delta // 3600)} 小时前在线"
    if delta < 86400 * 7:
        return f"{int(delta // 86400)} 天前在线"
    return time.strftime("%m月%d日", time.localtime(timestamp))


_ALLOWED_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_filename(name: str, fallback: str = "file") -> str:
    # Strip any path part with plain string handling: os.path.basename follows
    # platform rules, so on Windows "a:b?.zip" would lose its "a:" prefix as a
    # drive letter while Linux keeps it. Received filenames must sanitize the
    # same way on every platform.
    value = str(name).replace("\\", "/").rsplit("/", 1)[-1].strip()
    value = _ALLOWED_FILENAME.sub("_", value)
    value = value.strip(" .")
    if not value:
        value = fallback
    if len(value) > 180:
        stem, ext = os.path.splitext(value)
        ext = ext[:20]
        value = stem[: 180 - len(ext)] + ext
    return value


def looks_like_ipv6(value: str) -> bool:
    try:
        normalize_ipv6_host(value)
        return True
    except ValueError:
        return False


def _candidate_psutil_addresses() -> list[LocalAddress]:
    found: list[LocalAddress] = []
    if psutil is None:
        return found
    try:
        stats = psutil.net_if_stats()
        for interface, addrs in psutil.net_if_addrs().items():
            try:
                if not stats.get(interface) or not stats[interface].isup:
                    continue
            except Exception:
                pass
            for entry in addrs:
                if entry.family != socket.AF_INET6:
                    continue
                raw = entry.address
                base, zone = split_zone(raw)
                try:
                    parsed = ipaddress.IPv6Address(base)
                except ValueError:
                    continue
                if parsed.is_unspecified or parsed.is_multicast:
                    continue
                host = parsed.compressed
                if parsed.is_link_local:
                    zone = zone or interface
                    host = f"{host}%{zone}" if zone else host
                    kind = "link-local"
                elif parsed.is_loopback:
                    kind = "loopback"
                else:
                    kind = "global"
                found.append(LocalAddress(host=host, interface=interface, kind=kind))
    except Exception:
        return []
    return found


def _default_route_address() -> str | None:
    """Ask the OS which IPv6 source it would use without sending packets."""
    sock = None
    try:
        sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        sock.connect(("2001:4860:4860::8888", 53))
        address = sock.getsockname()[0]
        base, zone = split_zone(address)
        parsed = ipaddress.IPv6Address(base)
        if parsed.is_unspecified:
            return None
        return f"{parsed.compressed}%{zone}" if zone else parsed.compressed
    except OSError:
        return None
    finally:
        if sock is not None:
            sock.close()


def local_ipv6_addresses() -> list[LocalAddress]:
    """Return unique local IPv6 addresses, best candidates first."""
    candidates = _candidate_psutil_addresses()
    route = _default_route_address()
    base: list[LocalAddress] = []
    if route:
        base.append(LocalAddress(host=route, interface="默认路由", kind="global"))
    base.extend(candidates)

    ordered: list[LocalAddress] = []
    seen: set[str] = set()
    for entry in base:
        key = entry.host.casefold()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(entry)

    # Put global addresses first, then link-local, then loopback.
    rank = {"global": 0, "link-local": 1, "loopback": 2}
    ordered.sort(key=lambda item: rank.get(item.kind, 0))
    if not any(item.kind != "loopback" for item in ordered):
        ordered.append(LocalAddress(host="::1", interface="本机回环", kind="loopback"))
    return ordered


def shareable_ipv6_addresses() -> list[LocalAddress]:
    """Addresses that are meaningful to another device.

    Loopback addresses always refer to the local device. Link-local (fe80::/10)
    addresses only work on the same link and their scope/interface name is
    machine-specific. Neither is portable in a share card. Include only
    globally routable or uniquely local IPv6 addresses; advanced users can
    still add a scoped fe80 address manually for same-link tests.
    """
    unique: list[LocalAddress] = []
    seen: set[str] = set()
    for entry in local_ipv6_addresses():
        if entry.kind != "global":
            continue
        key = entry.host.casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(entry)
    return unique



