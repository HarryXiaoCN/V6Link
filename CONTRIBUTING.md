# 贡献指南

感谢你愿意改进 V6Link。这是一个以 Windows 桌面为主、面向 IPv6 直连场景的个人开源项目，欢迎 issue、文档修正和代码贡献。

## 开始之前

- 提交 issue 前先搜索是否已有相同问题。
- 较大的功能或架构改动，建议先开 issue 说明动机和方案，避免白做。
- **安全漏洞不要公开提 issue**，请按 [SECURITY.md](SECURITY.md) 的私密渠道报告。

## 开发环境

- Windows 10/11（主要目标）；Linux/macOS 可运行，但没有 DPAPI。
- Python **3.11+**。
- 需要本机启用 IPv6（测试会使用 `::1` 回环）。

```powershell
git clone https://github.com/HarryXiaoCN/V6Link.git
cd V6Link
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

Linux/macOS：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

## 运行测试

```powershell
$env:PYTHONPATH = "."
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

```bash
PYTHONPATH=. .venv/bin/python -m unittest discover -s tests -v
```

测试覆盖握手、身份固定、保险库加密、消息、文件确认/取消/续传、地址学习和好友间更新。新增协议行为时请同时补测试；改动加密或存储相关代码时，至少跑一遍全量测试。

## 代码风格

- 使用 [ruff](https://docs.astral.sh/ruff/)，行宽 100，目标 Python 3.11。
- 保留中文注释和用户可见的中文错误信息。
- 公共函数和方法尽量写类型标注，文件顶部使用 `from __future__ import annotations`。
- 不要提交 `.venv/`、`dist/`、`build/`、数据目录、日志或含真实身份/消息的文件。

```powershell
.\.venv\Scripts\python.exe -m ruff check v6link tests
```

当前仓库仍有少量历史 lint 提示（主要是中文全角标点的 `RUF001` 和行宽 `E501`），不要求一次清零，但不要新增未定义名、未使用导入或语法错误。

## 架构约定

- 网络引擎在 `v6link/engine/`。`ChatEngine` 是 facade，负责组装服务、生命周期和跨服务编排。
- 连接、消息、联系人、文件传输、更新、导出应各自属于对应服务；新增逻辑优先放进归属服务，不要重新把状态塞回 facade。
- 服务之间通过显式依赖交互；跨服务流程放在 facade。
- 事件统一通过事件总线发出，后台任务统一通过任务注册表创建，便于取消和测试。
- 传输层通过 `Transport` 抽象（当前是 IPv6 TCP）。如果要做中继、QUIC 或安卓端，优先扩展这一层，不要把新的传输细节写进业务服务。
- 详细分层和协议说明见 [docs/architecture.md](docs/architecture.md)。

## 提交与 Pull Request

- 提交信息使用 Conventional Commits：`feat:`、`fix:`、`docs:`、`refactor:`、`test:`、`chore:`。
- 一个 PR 尽量只做一件事；重构和行为变更分开。
- PR 描述请写清楚：问题、方案、如何测试、对协议/存储的影响。
- 提交前确认：
  - [ ] 全量测试通过；
  - [ ] `ruff check --select F,E9` 无错误；
  - [ ] 没有提交生成物或本地数据；
  - [ ] 涉及用户可见行为时更新了 README 或 CHANGELOG。

## 发布流程（维护者）

1. 同步更新 `v6link/__init__.py` 与 `pyproject.toml` 的版本号。
2. 更新 [CHANGELOG.md](CHANGELOG.md)。
3. 运行全量测试。
4. 构建 exe：

   ```powershell
   .\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean V6Link.spec
   ```

5. 在 GitHub Releases 附上 `dist/V6Link.exe` 和 SHA-256；不要直接把 exe 提交进仓库。

## 许可

向本项目提交代码即表示你同意以 [MIT](LICENSE) 许可发布你的贡献。
