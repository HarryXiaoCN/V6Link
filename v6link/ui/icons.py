"""Runtime-painted icon and avatar system.

Keeping the visual assets as inline SVG means the app is a self-contained
source checkout with no binary resource dependencies.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache

from PySide6.QtCore import QByteArray, QRectF, QSize, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtSvg import QSvgRenderer

_PATHS = {
    "chat": '<path d="M20.5 11.5a7.8 7.8 0 0 1-8 7.6 9.2 9.2 0 0 1-3.4-.6L4 20l1.6-4.3a7.2 7.2 0 0 1-1.2-4A7.8 7.8 0 0 1 12.5 4a7.8 7.8 0 0 1 8 7.5Z"/><path d="M8.7 10.7h7.6M8.7 14h4.9"/>',
    "card": '<rect x="3" y="5" width="18" height="14" rx="3"/><circle cx="9" cy="11" r="2"/><path d="M6.4 16c.5-1.5 1.5-2.3 2.7-2.3s2.2.8 2.7 2.3M14.5 10h4M14.5 13.5h3"/>',
    "settings": '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .34 1.87l.06.06-2.12 2.12-.06-.06a1.7 1.7 0 0 0-1.87-.34 1.7 1.7 0 0 0-1 1.55V20.5h-3v-.1a1.7 1.7 0 0 0-1.11-1.57 1.7 1.7 0 0 0-1.87.34l-.06.06-2.12-2.12.06-.06A1.7 1.7 0 0 0 7.2 15a1.7 1.7 0 0 0-1.55-1H5.5v-3h.1a1.7 1.7 0 0 0 1.57-1.11 1.7 1.7 0 0 0-.34-1.87l-.06-.06 2.12-2.12.06.06A1.7 1.7 0 0 0 10.8 6.2a1.7 1.7 0 0 0 1-1.55V4.5h3v.1a1.7 1.7 0 0 0 1.11 1.57 1.7 1.7 0 0 0 1.87-.34l.06-.06 2.12 2.12-.06.06A1.7 1.7 0 0 0 19.4 9a1.7 1.7 0 0 0 1.55 1h.1v3h-.1a1.7 1.7 0 0 0-1.55 1Z"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "search": '<circle cx="10.8" cy="10.8" r="6.3"/><path d="m16 16 4 4"/>',
    "paperclip": '<path d="m20 11.5-7.8 7.8a5.2 5.2 0 0 1-7.4-7.4l8.2-8.2a3.5 3.5 0 0 1 5 5l-8.2 8.2a1.8 1.8 0 0 1-2.5-2.5L14.7 7"/>',
    "send": '<path d="M21 3 10.5 13.5M21 3l-6.6 18-3.9-7.5L3 9.6 21 3Z"/>',
    "lock": '<rect x="4.5" y="10" width="15" height="10.5" rx="2.5"/><path d="M8 10V7.5a4 4 0 0 1 8 0V10M12 14v2.5"/>',
    "more": '<circle cx="5" cy="12" r="1.3" fill="{color}" stroke="none"/><circle cx="12" cy="12" r="1.3" fill="{color}" stroke="none"/><circle cx="19" cy="12" r="1.3" fill="{color}" stroke="none"/>',
    "copy": '<rect x="8.5" y="8.5" width="11" height="11" rx="2"/><path d="M15.5 8.5v-2a2 2 0 0 0-2-2h-7a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h2"/>',
    "external": '<path d="M14 4h6v6M20 4l-9 9"/><path d="M18 13.5V18a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4.5"/>',
    "folder": '<path d="M3.5 8.5A2.5 2.5 0 0 1 6 6h3.2l1.8 2h7A2.5 2.5 0 0 1 20.5 10.5v6A2.5 2.5 0 0 1 18 19H6a2.5 2.5 0 0 1-2.5-2.5v-8Z"/>',
    "download": '<path d="M12 4v11M7.5 10.5 12 15l4.5-4.5M5 19.5h14"/>',
    "close": '<path d="m6 6 12 12M18 6 6 18"/>',
    "check": '<path d="m4.5 12.5 5 5 10-11"/>',
    "check_double": '<path d="m2.5 12.5 4.5 4.5 8-9M11 16l1.5 1 8-9"/>',
    "alert": '<path d="M12 3.8 21 20H3L12 3.8Z"/><path d="M12 9v5M12 17.2v.2"/>',
    "shield": '<path d="M12 3 19.5 6v5.4c0 4.4-2.9 7.7-7.5 9.6-4.6-1.9-7.5-5.2-7.5-9.6V6L12 3Z"/><path d="m9 12 2 2 4-4"/>',
    "network": '<circle cx="12" cy="12" r="9"/><path d="M3.3 12h17.4M12 3a15 15 0 0 1 0 18M12 3a15 15 0 0 0 0 18"/>',
    "users": '<circle cx="9" cy="8.5" r="3.2"/><path d="M3 19.5c.4-3.6 2.7-5.7 6-5.7s5.6 2.1 6 5.7M16.5 5.7a3 3 0 0 1 0 5.7M19 14.2c1.4.9 2.3 2.6 2.5 4.9"/>',
    "edit": '<path d="M14 5.5 18.5 10M4 20l.9-5.2L16.2 3.5a2.2 2.2 0 0 1 3.1 0l1.2 1.2a2.2 2.2 0 0 1 0 3.1L9.2 19.1 4 20Z"/>',
    "trash": '<path d="M4 7h16M9 7V4.5h6V7M6.5 7l.8 12a2 2 0 0 0 2 1.9h5.4a2 2 0 0 0 2-1.9l.8-12M10 11v6M14 11v6"/>',
    "chevron_right": '<path d="m9 5 7 7-7 7"/>',
    "chevron_left": '<path d="m15 5-7 7 7 7"/>',
    "retry": '<path d="M20 11a8 8 0 1 0-2.2 5.8M20 4v7h-7"/>',
    "file": '<path d="M14 3H6.5A1.5 1.5 0 0 0 5 4.5v15A1.5 1.5 0 0 0 6.5 21h11a1.5 1.5 0 0 0 1.5-1.5V8l-5-5Z"/><path d="M14 3v5h5"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 7.7v.2"/>',
    "key": '<circle cx="8" cy="15" r="4"/><path d="m11 12 8-8M16 7l2 2M18 5l2 2"/>',
    "arrow_down": '<path d="M12 4v16M6 14l6 6 6-6"/>',
    "sparkles": '<path d="m12 3 1.4 4.1L17.5 8.5l-4.1 1.4L12 14l-1.4-4.1-4.1-1.4 4.1-1.4L12 3ZM19 14l.8 2.2L22 17l-2.2.8L19 20l-.8-2.2L16 17l2.2-.8L19 14Z"/>',
    "wifi_off": '<path d="M3 3l18 18M8.5 15.5a5 5 0 0 1 6.8-.2M5 12a11 11 0 0 1 4.2-2.5M19.3 12a11 11 0 0 0-2.8-1.8M1.8 8.5A16 16 0 0 1 7.2 5.2M22.2 8.5a16 16 0 0 0-7.8-3.3M12 19h.01"/>',
}


def _svg(name: str, color: str) -> bytes:
    path = _PATHS[name]
    path = path.replace("{color}", color)
    body = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
        f'stroke="{color}" stroke-width="1.7" stroke-linecap="round" '
        f'stroke-linejoin="round">{path}</svg>'
    )
    return body.encode("utf-8")


@lru_cache(maxsize=256)
def icon(name: str, color: str = "#AFC2D1", size: int = 20) -> QIcon:
    renderer = QSvgRenderer(QByteArray(_svg(name, color)))
    ratio = 2.0
    pixmap = QPixmap(QSize(int(size * ratio), int(size * ratio)))
    pixmap.fill(Qt.transparent)
    pixmap.setDevicePixelRatio(ratio)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    renderer.render(painter, QRectF(0, 0, size, size))
    painter.end()
    return QIcon(pixmap)


_GRADIENTS = (
    ("#2BB9A4", "#176B87"),
    ("#6A7BF5", "#383F99"),
    ("#E99B57", "#9B5332"),
    ("#D86C9C", "#783651"),
    ("#48B378", "#266847"),
    ("#6D8FAF", "#374D65"),
    ("#B47CE3", "#623C86"),
    ("#E0A63A", "#8F6122"),
)


def _initials(name: str) -> str:
    clean = (name or "?").strip()
    if not clean:
        return "?"
    if all(ord(ch) < 128 for ch in clean):
        parts = [part for part in clean.replace("_", " ").replace("-", " ").split() if part]
        if len(parts) >= 2:
            return (parts[0][0] + parts[1][0]).upper()
        return clean[:2].upper()
    return clean[0]


@lru_cache(maxsize=512)
def avatar_pixmap(name: str, identifier: str, size: int = 44) -> QPixmap:
    digest = hashlib.sha256((identifier or name or "V6Link").encode("utf-8")).digest()
    start, end = _GRADIENTS[digest[0] % len(_GRADIENTS)]
    ratio = 2.0
    pixmap = QPixmap(QSize(int(size * ratio), int(size * ratio)))
    pixmap.fill(Qt.transparent)
    pixmap.setDevicePixelRatio(ratio)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    rect = QRectF(0, 0, size, size)
    gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
    gradient.setColorAt(0.0, QColor(start))
    gradient.setColorAt(1.0, QColor(end))
    painter.setPen(Qt.NoPen)
    painter.setBrush(gradient)
    painter.drawEllipse(rect)
    painter.setPen(QPen(QColor(255, 255, 255, 32), 1))
    painter.setBrush(Qt.NoBrush)
    painter.drawEllipse(rect.adjusted(0.5, 0.5, -0.5, -0.5))
    font = QFont("Microsoft YaHei UI")
    font.setPixelSize(max(10, int(size * 0.34)))
    font.setWeight(QFont.DemiBold)
    painter.setFont(font)
    painter.setPen(QColor("#F7FFFD"))
    painter.drawText(rect.adjusted(0, -1, 0, -1), Qt.AlignCenter, _initials(name))
    painter.end()
    return pixmap


@lru_cache(maxsize=8)
def logo_pixmap(size: int = 42) -> QPixmap:
    ratio = 2.0
    pixmap = QPixmap(QSize(int(size * ratio), int(size * ratio)))
    pixmap.fill(Qt.transparent)
    pixmap.setDevicePixelRatio(ratio)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    rect = QRectF(0, 0, size, size)
    gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
    gradient.setColorAt(0.0, QColor("#4CF2D3"))
    gradient.setColorAt(1.0, QColor("#1B8DB1"))
    path = QPainterPath()
    path.addRoundedRect(rect, size * 0.28, size * 0.28)
    painter.fillPath(path, gradient)

    # Two interlocking links, drawn unmistakably as the V6Link mark.
    pen = QPen(QColor("#06241F"), max(1.5, size * 0.065))
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    painter.save()
    painter.translate(size * 0.5, size * 0.5)
    painter.rotate(-38)
    link_w = size * 0.38
    link_h = size * 0.21
    painter.drawRoundedRect(QRectF(-link_w * 0.72, -link_h / 2, link_w, link_h), link_h / 2, link_h / 2)
    painter.drawRoundedRect(QRectF(-link_w * 0.28, -link_h / 2, link_w, link_h), link_h / 2, link_h / 2)
    painter.restore()
    painter.setPen(QPen(QColor("#D9FFF7"), max(1, size * 0.035)))
    painter.drawLine(
        int(size * 0.31),
        int(size * 0.72),
        int(size * 0.69),
        int(size * 0.72),
    )
    painter.end()
    return pixmap
