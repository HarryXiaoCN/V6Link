# 第三方组件说明

V6Link 本体的源代码以 [MIT](LICENSE) 许可发布。运行和分发 V6Link 时会用到以下第三方组件，它们各自保留原有许可。

## 运行时依赖

| 组件 | 用途 | 许可 |
|---|---|---|
| [PySide6](https://pypi.org/project/PySide6/) / [Qt for Python](https://www.qt.io/qt-for-python) | 桌面界面 | LGPL-3.0（另有商业许可） |
| [Qt 6](https://www.qt.io/) | PySide6 的底层框架 | LGPL-3.0（另有商业许可） |
| [cryptography](https://cryptography.io/) | Ed25519、X25519、HKDF、AES-GCM | Apache-2.0 或 BSD-3-Clause |
| [psutil](https://github.com/giampaolo/psutil) | 枚举本机 IPv6 地址 | BSD-3-Clause |
| [Python](https://www.python.org/) | 运行时 | PSF License |

## 构建工具

| 组件 | 用途 | 许可 |
|---|---|---|
| [PyInstaller](https://pyinstaller.org/) | 生成 Windows 单文件 exe | GPL-2.0-or-later，附「bootloader 例外」条款 |

PyInstaller 的例外条款允许以任意许可分发由它打包的程序；这不改变 V6Link 本体的 MIT 许可。

## 分发 PySide6 / Qt 时的 LGPL 义务

`dist/V6Link.exe` 使用 PyInstaller 单文件模式打包了 PySide6 和 Qt 动态库。以二进制形式分发时，LGPL-3.0 要求：

1. 随附 LGPL-3.0 与 Qt/PySide6 的版权声明（本文件即为此目的）；
2. 允许接收者替换或重新链接 LGPL 组件。

如果你要再分发这个 exe，建议同时提供：

- 本文件与 [LICENSE](LICENSE)；
- PySide6 和 Qt 的 LGPL-3.0 许可文本；
- 获取对应版本 Qt/PySide6 源码的方式（PySide6 与 Qt 的官方发布页）；
- 可复现的构建说明（见 [README](README.md) 的「打包 Windows exe」与 [packaging/build_exe.ps1](packaging/build_exe.ps1)）。

如果所在组织对 LGPL 合规要求严格，可以改为「onedir」模式分发，或在目标系统上使用动态链接的 Qt。

## 资产

`assets/V6Link.ico`、`screenshots/` 下的界面截图以及 `v6link/ui/icons.py` 中的内联 SVG 由项目作者制作，随项目以 MIT 许可发布。截图中的好友名称、消息和文件均为演示数据。
