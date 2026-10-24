"""Share-card parsing shared by the UI and tests (no Qt dependency)."""

from __future__ import annotations

import ipaddress
import re
from typing import Any

from .netutil import parse_endpoint


def normalize_peer_id(text: str) -> str:
    compact = re.sub(r"[^0-9a-fA-F]", "", text or "").lower()
    if compact and not re.fullmatch(r"[0-9a-f]{64}", compact):
        raise ValueError("身份指纹应为 64 位十六进制")
    return compact


def parse_share_card(text: str) -> dict[str, Any]:
    """Parse the human-readable card copied from MyCardDialog."""
    if not text or "V6Link" not in text:
        raise ValueError("剪贴板中没有可识别的 V6Link 加密名片")
    name = ""
    peer_id = ""
    addresses: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        name_match = re.match(r"^(?:昵称|名字)\s*[:：]\s*(.+)$", line)
        if name_match:
            name = name_match.group(1).strip()
            continue
        peer_match = re.match(r"^身份指纹\s*[:：]\s*(.+)$", line)
        if peer_match:
            peer_id = normalize_peer_id(peer_match.group(1))
            continue
        endpoint_match = re.search(r"(\[[^\]]+\]:\d+)", line)
        if endpoint_match:
            host, port = parse_endpoint(endpoint_match.group(1))
            try:
                parsed_host = ipaddress.IPv6Address(host.split("%", 1)[0])
            except ValueError:
                continue
            # Loopback points at the receiver itself; link-local addresses only
            # make sense with a locally correct interface scope. Neither is
            # portable between machines, including cards from older versions.
            if parsed_host.is_loopback or parsed_host.is_link_local:
                continue
            key = (host.casefold(), port)
            if key not in seen:
                seen.add(key)
                addresses.append(
                    {
                        "host": host,
                        "port": port,
                        "source": "manual",
                        "label": "名片导入",
                    }
                )
    if not addresses:
        raise ValueError("名片中没有找到可跨设备使用的 IPv6 地址（回环和链路本地地址已忽略）")
    return {"name": name, "peer_id": peer_id, "addresses": addresses}
