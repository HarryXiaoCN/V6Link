"""V6Link visual language: palette, typography and application style sheet."""

from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette
from PySide6.QtWidgets import QApplication


class C:
    BG = "#080D13"
    BG_SOFT = "#0B1118"
    RAIL = "#080D13"
    SIDEBAR = "#0D141D"
    PANEL = "#101923"
    SURFACE = "#15212D"
    SURFACE_2 = "#1A2937"
    SURFACE_3 = "#213344"
    BORDER = "#223142"
    BORDER_SOFT = "#1A2734"
    TEXT = "#ECF4F8"
    TEXT_SOFT = "#C4D2DE"
    MUTED = "#7F93A5"
    MUTED_2 = "#5C7082"
    ACCENT = "#3DDFC0"
    ACCENT_HOVER = "#5CEACF"
    ACCENT_DIM = "#1A6F60"
    ACCENT_INK = "#06241F"
    BLUE = "#5AB8FF"
    OUT_BUBBLE = "#133D37"
    OUT_BORDER = "#1C554B"
    IN_BUBBLE = "#16232F"
    IN_BORDER = "#223444"
    WARNING = "#F3B65C"
    DANGER = "#FF7383"
    SUCCESS = "#53E6A1"


STYLE = """
* {
    outline: none;
}
QWidget {
    color: #ECF4F8;
    font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
    font-size: 13px;
}
QWidget#root, QMainWindow {
    background: #080D13;
}
QFrame#rail {
    background: #080D13;
    border: none;
}
QFrame#sidebar {
    background: #0D141D;
    border: none;
    border-left: 1px solid #111C27;
    border-right: 1px solid #1A2733;
}
QFrame#chatPanel {
    background: #0A1017;
    border: none;
}
QLabel {
    background: transparent;
    border: none;
}
QLabel[role="h1"] {
    font-size: 23px;
    font-weight: 700;
    color: #ECF4F8;
}
QLabel[role="h2"] {
    font-size: 18px;
    font-weight: 700;
    color: #ECF4F8;
}
QLabel[role="h3"] {
    font-size: 15px;
    font-weight: 600;
    color: #ECF4F8;
}
QLabel[role="muted"] {
    color: #7F93A5;
    font-size: 12px;
}
QLabel[role="tiny"] {
    color: #677D90;
    font-size: 11px;
}
QLabel[role="mono"] {
    font-family: "Cascadia Mono", "Consolas", monospace;
    color: #A9BDCB;
    font-size: 11px;
    letter-spacing: 1px;
}
QLabel[role="accent"] {
    color: #3DDFC0;
    font-size: 12px;
    font-weight: 600;
}
QLabel[role="eyebrow"] {
    color: #3DDFC0;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 2px;
}
QLabel[role="danger"] {
    color: #FF7383;
    font-size: 12px;
}
QLabel[role="warning"] {
    color: #F3B65C;
    font-size: 12px;
}

QPushButton {
    border: 1px solid #283A4B;
    border-radius: 10px;
    background: #15212D;
    color: #DCE8F0;
    padding: 8px 16px;
    font-weight: 500;
}
QPushButton:hover {
    background: #1B2A38;
    border-color: #354C60;
}
QPushButton:pressed {
    background: #111C27;
}
QPushButton:disabled {
    color: #5C7082;
    background: #111A23;
    border-color: #1C2834;
}
QPushButton#primaryButton {
    background: #3DDFC0;
    border: 1px solid #3DDFC0;
    color: #06241F;
    font-weight: 700;
    padding: 9px 18px;
}
QPushButton#primaryButton:hover {
    background: #5CEACF;
    border-color: #5CEACF;
}
QPushButton#primaryButton:pressed {
    background: #2CC5AA;
}
QPushButton#dangerButton {
    color: #FF8A96;
    border-color: #4B2A33;
    background: #24161B;
}
QPushButton#dangerButton:hover {
    background: #301A20;
    border-color: #6E3743;
}
QPushButton#ghostButton {
    background: transparent;
    border-color: transparent;
    color: #9FB1C0;
    padding: 7px 10px;
}
QPushButton#ghostButton:hover {
    background: #16212C;
    color: #ECF4F8;
}
QPushButton#softButton {
    background: #17232F;
    border: 1px solid #24384B;
    color: #CDDCE7;
}

QToolButton {
    background: transparent;
    border: none;
    border-radius: 10px;
    padding: 7px;
}
QToolButton:hover {
    background: #1A2733;
}
QToolButton:pressed {
    background: #223242;
}
QToolButton#railButton {
    border-radius: 14px;
    padding: 0px;
}
QToolButton#railButton:hover {
    background: #111C27;
}
QToolButton#railButton:checked {
    background: #152A2A;
}
QToolButton#iconButton {
    border-radius: 9px;
    background: #131F2A;
    border: 1px solid #1E2E3D;
}
QToolButton#iconButton:hover {
    background: #1A2A38;
    border-color: #2A4154;
}
QToolButton#plainIcon:hover {
    background: #1A2733;
}
QToolButton#closeButton {
    color: #8CA0B2;
}
QToolButton#closeButton:hover {
    background: #35202A;
}

QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QComboBox {
    background: #0F1821;
    border: 1px solid #223242;
    border-radius: 10px;
    color: #E8F1F6;
    selection-background-color: #267C6E;
    selection-color: #FFFFFF;
    padding: 8px 11px;
}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus, QComboBox:focus {
    border: 1px solid #2E8F7C;
    background: #101C26;
}
QLineEdit:disabled, QPlainTextEdit:disabled, QSpinBox:disabled {
    color: #647A8B;
}
QLineEdit#searchInput {
    background: #111B25;
    border: 1px solid #1A2937;
    border-radius: 11px;
    padding: 9px 12px 9px 34px;
    color: #DDE9F1;
}
QLineEdit#searchInput:focus {
    border: 1px solid #2B4C54;
    background: #121E28;
}
QPlainTextEdit#composer {
    background: transparent;
    border: none;
    padding: 2px 0px;
    font-size: 14px;
}
QSpinBox {
    padding-right: 20px;
}
QSpinBox::up-button, QSpinBox::down-button {
    width: 16px;
    border: none;
    background: transparent;
}
QSpinBox::up-arrow {
    image: none;
    border-left: 3px solid transparent;
    border-right: 3px solid transparent;
    border-bottom: 4px solid #7F93A5;
}
QSpinBox::down-arrow {
    image: none;
    border-left: 3px solid transparent;
    border-right: 3px solid transparent;
    border-top: 4px solid #7F93A5;
}
QComboBox::drop-down { border: none; width: 20px; }
QComboBox QAbstractItemView {
    background: #14202B;
    border: 1px solid #26394A;
    selection-background-color: #1E3B3A;
    padding: 4px;
}

QScrollArea {
    background: transparent;
    border: none;
}
QScrollArea > QWidget > QWidget {
    background: transparent;
}
QScrollBar:vertical {
    background: transparent;
    width: 8px;
    margin: 3px 1px;
}
QScrollBar::handle:vertical {
    background: #263B4D;
    border-radius: 4px;
    min-height: 30px;
}
QScrollBar::handle:vertical:hover {
    background: #345069;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
    background: transparent;
    border: none;
    height: 0px;
}
QScrollBar:horizontal {
    background: transparent;
    height: 8px;
}
QScrollBar::handle:horizontal {
    background: #263B4D;
    border-radius: 4px;
    min-width: 30px;
}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal,
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
    background: transparent;
    border: none;
    width: 0px;
}

QFrame#friendItem {
    border: 1px solid transparent;
    border-radius: 13px;
    background: transparent;
}
QFrame#friendItem:hover {
    background: #121F2A;
    border-color: #172836;
}
QFrame#friendItem[selected="true"] {
    background: #152826;
    border: 1px solid #1D3A35;
}
QFrame#friendItem[selected="true"]:hover {
    background: #17302C;
}

QFrame#card {
    background: #101B25;
    border: 1px solid #1E2E3D;
    border-radius: 16px;
}
QFrame#softCard {
    background: #0F1922;
    border: 1px solid #1A2A38;
    border-radius: 14px;
}
QFrame#requestBanner {
    background: #102520;
    border: 1px solid #1D443B;
    border-radius: 14px;
}
QFrame#networkBanner {
    background: #211B12;
    border: 1px solid #47371D;
    border-radius: 12px;
}
QFrame#divider {
    background: #192632;
    border: none;
    max-height: 1px;
    min-height: 1px;
}
QFrame#headerDivider {
    background: #15222D;
    border: none;
    max-height: 1px;
    min-height: 1px;
}
QFrame#composerCard {
    background: #101A23;
    border: 1px solid #1F3040;
    border-radius: 16px;
}
QFrame#composerCard[dragOver="true"] {
    border: 1px solid #3DDFC0;
    background: #11231F;
}
QFrame#messageIn {
    background: #16232F;
    border: 1px solid #223444;
    border-radius: 14px;
}
QFrame#messageOut {
    background: #133D37;
    border: 1px solid #1C554B;
    border-radius: 14px;
}
QFrame#fileCardIn {
    background: #16232F;
    border: 1px solid #223444;
    border-radius: 15px;
}
QFrame#fileCardOut {
    background: #12342F;
    border: 1px solid #1A4A42;
    border-radius: 15px;
}
QFrame#fileIcon {
    background: #1E3745;
    border: 1px solid #2A4858;
    border-radius: 10px;
}
QFrame#fileIconOut {
    background: #1A443D;
    border: 1px solid #246055;
    border-radius: 10px;
}
QLabel#fileType {
    color: #7FE3D2;
    font-size: 9px;
    font-weight: 800;
}
QFrame#datePill {
    background: #111E29;
    border: 1px solid #1C2C3A;
    border-radius: 10px;
}
QLabel#dateText {
    color: #6D8497;
    font-size: 11px;
    font-weight: 500;
}
QLabel#bubbleText {
    color: #E6EFF5;
    font-size: 14px;
    line-height: 1.3;
}
QLabel#bubbleMeta {
    color: #7190A1;
    font-size: 10px;
}
QLabel#bubbleMetaOut {
    color: #71B6A9;
    font-size: 10px;
}
QProgressBar#transferBar {
    border: none;
    background: #263B44;
    border-radius: 2px;
    height: 4px;
    text-align: center;
}
QProgressBar#transferBar::chunk {
    background: #3DDFC0;
    border-radius: 2px;
}
QToolButton#fileAction {
    color: #8FC7BE;
    background: #1A3A34;
    border: 1px solid #22564A;
    border-radius: 7px;
    padding: 4px 9px;
    font-size: 11px;
}
QToolButton#fileAction:hover {
    background: #225248;
    color: #E1FFF9;
}

QMenu {
    background: #101B25;
    border: 1px solid #243646;
    border-radius: 10px;
    padding: 6px;
}
QMenu::item {
    padding: 8px 26px 8px 12px;
    border-radius: 7px;
    color: #D8E4EC;
}
QMenu::item:selected {
    background: #1A2C39;
    color: #FFFFFF;
}
QMenu::separator {
    height: 1px;
    background: #1E2C39;
    margin: 5px 8px;
}
QToolTip {
    background: #172531;
    color: #E5EFF5;
    border: 1px solid #2D4356;
    border-radius: 7px;
    padding: 6px 9px;
}

QDialog {
    background: #0C141C;
}
QFrame#dialogCard {
    background: #0C141C;
    border: none;
}
QFrame#dialogSection {
    background: #101B25;
    border: 1px solid #1E2E3D;
    border-radius: 13px;
}
QFrame#addressRow {
    background: #101B25;
    border: 1px solid #1C2C3A;
    border-radius: 10px;
}
QFrame#identityCard {
    background: #101F22;
    border: 1px solid #1D3E3A;
    border-radius: 13px;
}
QFrame#requestCard {
    background: #111C26;
    border: 1px solid #21303F;
    border-radius: 14px;
}
QFrame#welcomeFeature {
    background: #0E1821;
    border: 1px solid #192936;
    border-radius: 13px;
}

QCheckBox {
    color: #C2D1DB;
    font-size: 12px;
    spacing: 8px;
}
QCheckBox::indicator {
    width: 16px;
    height: 16px;
    border-radius: 5px;
    border: 1px solid #354B5E;
    background: #0F1922;
}
QCheckBox::indicator:hover {
    border-color: #4E6A80;
}
QCheckBox::indicator:checked {
    background: #3DDFC0;
    border-color: #3DDFC0;
    image: none;
}
QSlider::groove:horizontal {
    height: 4px;
    background: #233543;
    border-radius: 2px;
}
QSlider::sub-page:horizontal {
    background: #3DDFC0;
    border-radius: 2px;
}
QSlider::handle:horizontal {
    width: 16px;
    height: 16px;
    margin: -6px 0px;
    border-radius: 8px;
    background: #E6FFFA;
    border: 2px solid #3DDFC0;
}
QMessageBox {
    background: #0D161F;
}
QMessageBox QLabel {
    color: #E7F0F6;
}
QFileDialog {
    background: #0D161F;
}
QPushButton#linkButton {
    background: transparent;
    border: none;
    color: #63C9BB;
    padding: 2px;
    text-align: left;
    font-size: 11px;
    font-weight: 600;
}
QPushButton#linkButton:hover {
    color: #8DF0E1;
}
QLabel#unreadBadge {
    background: #3DDFC0;
    color: #05251F;
    border-radius: 9px;
    font-size: 9px;
    font-weight: 800;
    padding: 0px 4px;
}
QPushButton#sendButton {
    background: #3DDFC0;
    border: none;
    border-radius: 12px;
    padding: 0px;
}
QPushButton#sendButton:hover {
    background: #63EFD7;
}
QPushButton#sendButton:pressed {
    background: #2BC8AE;
}
QPushButton#sendButton:disabled {
    background: #18312E;
}
QFrame#statusPill {
    background: #0E201E;
    border: 1px solid #1B3A35;
    border-radius: 12px;
}
QFrame#settingsSection {
    background: #101B25;
    border: 1px solid #1E2E3D;
    border-radius: 14px;
}
QLabel#settingsTitle {
    font-size: 12px;
    font-weight: 600;
    color: #D9E7EF;
}
QLabel#settingsHint {
    font-size: 10px;
    color: #708798;
}
QLineEdit#fingerprintInput {
    font-family: "Cascadia Mono", "Consolas", monospace;
    font-size: 11px;
    letter-spacing: 1px;
}
QFrame#addressRow QLineEdit {
    background: #0D1821;
    border: 1px solid #1D2D3B;
    border-radius: 8px;
    padding: 7px 9px;
}
QFrame#addressRow QSpinBox {
    background: #0D1821;
    border: 1px solid #1D2D3B;
    border-radius: 8px;
    padding: 7px 6px;
}
QDialog QLabel {
    color: #CAD8E2;
}
QDialog QFrame#dialogSection {
    background: #0F1922;
    border: 1px solid #1C2C3A;
    border-radius: 13px;
}
QPushButton#dialogDanger {
    color: #FF98A4;
    background: #23161B;
    border: 1px solid #3F2730;
}
QLabel#errorHint {
    color: #FF8C99;
    font-size: 11px;
    padding: 2px;
}
QFrame#toastFrame {
    background: #13212B;
    border: 1px solid #273D4D;
    border-radius: 12px;
}
QLabel#toastText {
    color: #DDEAF1;
    font-size: 11px;
}
QFrame#toastFrame[level="error"] {
    border: 1px solid #503039;
    background: #21171C;
}
QFrame#toastFrame[level="warning"] {
    border: 1px solid #4D3E26;
    background: #211D16;
}
QFrame#toastFrame[level="success"] {
    border: 1px solid #20473E;
    background: #12201D;
}
QFrame#contactRequestRow {
    background: #101B25;
    border: 1px solid #1E2F3E;
    border-radius: 13px;
}
"""


def _pick_font() -> str:
    available = set(QFontDatabase.families())
    for candidate in ("Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", "Segoe UI"):
        if candidate in available:
            return candidate
    return "Sans Serif"


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    font_name = _pick_font()
    base = QFont(font_name, 10)
    base.setStyleStrategy(QFont.PreferAntialias)
    app.setFont(base)

    palette = QPalette()
    palette.setColor(QPalette.Window, QColor(C.BG))
    palette.setColor(QPalette.WindowText, QColor(C.TEXT))
    palette.setColor(QPalette.Base, QColor(C.PANEL))
    palette.setColor(QPalette.AlternateBase, QColor(C.SURFACE))
    palette.setColor(QPalette.Text, QColor(C.TEXT))
    palette.setColor(QPalette.Button, QColor(C.SURFACE))
    palette.setColor(QPalette.ButtonText, QColor(C.TEXT))
    palette.setColor(QPalette.Highlight, QColor(C.ACCENT_DIM))
    palette.setColor(QPalette.HighlightedText, QColor(C.TEXT))
    palette.setColor(QPalette.ToolTipBase, QColor(C.SURFACE_2))
    palette.setColor(QPalette.ToolTipText, QColor(C.TEXT))
    palette.setColor(QPalette.PlaceholderText, QColor(C.MUTED_2))
    palette.setColor(QPalette.Link, QColor(C.ACCENT))
    app.setPalette(palette)
    app.setStyleSheet(STYLE)


def set_role(widget, role: str) -> None:
    widget.setProperty("role", role)
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def refresh_style(widget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


