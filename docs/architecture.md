# 架构说明

本文描述 V6Link 的进程结构、网络引擎分层和主要数据流。安全边界与密码学细节见 [SECURITY.md](../SECURITY.md)，贡献约定见 [CONTRIBUTING.md](../CONTRIBUTING.md)。

## 1. 总体结构

V6Link 是一个单进程桌面应用，分为三层：

```
┌──────────────────────────────────────────────┐
│ UI 层（PySide6，主线程）                      │
│   main_window / dialogs / widgets / tray     │
└───────────────┬──────────────────────────────┘
                │ 线程安全的调用与事件桥
┌───────────────▼──────────────────────────────┐
│ 运行时层（BackendRuntime）                    │
│   独立线程 + asyncio 事件循环                 │
│   run_coroutine_threadsafe / 事件队列         │
└───────────────┬──────────────────────────────┘
                │
┌───────────────▼──────────────────────────────┐
│ 网络引擎（v6link/engine/）                    │
│   ChatEngine（facade）                        │
│   ├─ ConnectionManager   连接、握手、会话     │
│   ├─ ContactService      联系人、地址学习     │
│   ├─ MessagingService    文本与好友通知       │
│   ├─ FileTransferService 文件收发、续传、导出 │
│   ├─ UpdateService       好友间版本更新       │
│   ├─ EventBus            事件与节流           │
│   └─ TaskRegistry        后台任务生命周期     │
└───────────────┬──────────────────────────────┘
                │
┌───────────────▼──────────────────────────────┐
│ 基础层                                        │
│   crypto / keyvault / vault / filestore       │
│   netutil / updates（本地更新包处理）          │
└──────────────────────────────────────────────┘
```

UI 永远不直接触碰 asyncio 对象。所有网络操作都通过 `BackendRuntime` 投递到后端线程，事件再经 Qt 队列回到主线程。

## 2. 引擎的服务化边界

`ChatEngine` 是唯一的公共入口，负责组装服务和跨服务编排，不承载具体协议状态。

| 组件 | 拥有的状态 | 主要职责 |
|---|---|---|
| `EventBus` | 事件接收器、联系人事件节流表 | 统一发出 UI 事件，隔离 sink 异常 |
| `TaskRegistry` | 后台任务集合 | 创建、跟踪、取消 asyncio 任务 |
| `ConnectionManager` | 监听 server、活动会话集合、本机端口 | 监听、发起连接、握手、hello、地址学习、入站命令交给路由 |
| `ContactService` | 无独立状态（基于保险库） | 联系人 CRUD、待处理请求、入站身份匹配、公告地址合并 |
| `MessagingService` | 已接受好友集合、自动重试状态 | 文本收发与去重、好友接受通知、等待消息重试 |
| `FileTransferService` | 入站/出站传输表、文件锁、下载目录 | 文件邀请与确认、并行通道、分块校验、暂停/续传/取消、导出 |
| `UpdateService` | 出站/入站更新邀请表 | 更新包签名、拉取、暂存、进度与结果事件 |
| `CommandRouter` | 无状态 | 按加密控制帧的 `type` 分发到对应服务 |

依赖方向是单向的：

```
core → router → messaging / files / updates
core → connections → transport
connections → contacts
messaging / files / updates → contacts + connections + events + tasks
all services → vault / models
```

服务之间不通过 facade 互相回调；确需跨服务的流程（设置变更、接受好友、重试消息、关闭应用）由 `ChatEngine` 编排。

## 3. 连接生命周期

1. 启动时 `ChatEngine.start()` 让 `ConnectionManager` 在 `::` 上监听 TCP 端口。
2. 主动连接：按联系人保存的地址顺序尝试，建立 TCP 后执行 Ed25519 + X25519 握手，验证对端身份指纹，再交换加密 hello（昵称、监听端口、当前 IPv6 地址、版本）。
3. 入站连接：完成握手后读取 hello，`ContactService` 按指纹匹配联系人；未知身份生成待处理好友请求；身份冲突直接拒绝并发出安全告警。
4. 接受后，连接交给 `CommandRouter` 处理一条命令，随后关闭；文件数据通道是额外的短连接，各自重新握手。

每条连接都有独立会话密钥和单调 nonce，详见 SECURITY.md。

## 4. 主要数据流

### 文本消息

```
发送方 UI → send_text → 保险库写入 sending 消息
        → MessagingService 连接对端
        → 加密 text 控制帧 → 对端去重、落库
        → text_ack → 发送方标记 sent
```

对方尚未接受好友请求时，消息保持 `waiting`，收到 `friend_accept` 后自动重试。

### 文件传输

```
发送方 file_offer（名称/大小/SHA-256/分块参数）
接收方检查磁盘、确认或拒绝；确认时返回已收分块位图
发送方开 N 条数据连接，每条先发 stream_open，再发 PAYLOAD_FILE 分块
接收方按 offset 写入加密容器，逐块 AEAD 校验
所有分块到齐 → 校验大小/分块数 → 自动导出到下载目录
```

- 取消会删除分片且不能续传。
- 断网、对端离线或应用退出转为暂停，保留已认证分片；重启后扫描位图续传。
- 发送方源文件 SHA-256 变化后禁止续传。

### 好友间更新

```
发送方 push_update：签名 manifest（offer_id/版本/大小/SHA-256/文件名/说明）
接收方验证签名与版本，展示邀请；用户接受后才连接回发送方拉取
发送方流式发送 PAYLOAD_FILE；接收方逐块校验、落盘到 updates/pending
接收方再次校验 SHA-256 与签名，然后才允许安装
Windows 单文件 exe：生成替换脚本，退出后替换、保留备份、重启
```

更新包不经过任何服务器，也没有官方更新源；可信度等于对发送方身份指纹的信任。

## 5. 传输抽象

`TcpTransport` 负责 socket 选项、监听和建连；`ConnectionManager` 负责其上的加密握手与 hello。把传输细节收敛到这一层，是为了后续可以增加：

- 中继/兜底通道（移动网络或企业网络通常无法入站直连）；
- QUIC 或 WebRTC 数据通道（低延迟、跨 NAT）；
- 安卓客户端的本地实现。

新增传输时不应改动 `MessagingService`、`FileTransferService` 的协议逻辑。

## 6. 本地存储

- 保险库 `vault.v6`：联系人、消息、地址、身份私钥、文件密钥、设置，整体 AES-256-GCM。
- 主密钥：Windows 用 DPAPI；其他平台是 `0600` 文件。
- 附件：`attachments/*.v6f`，每个文件独立随机密钥，按固定 slot 加密。
- 更新暂存：`updates/incoming`（未完成分片）与 `updates/pending`（已校验、待安装）。

## 7. 线程与并发规则

- 引擎内所有状态只在后端事件循环线程中修改，不使用跨线程锁。
- UI 通过 `run_coroutine_threadsafe` 提交调用，通过队列接收事件。
- 文件 I/O、哈希、加密导出使用 `asyncio.to_thread`，避免阻塞事件循环。
- 取消和暂停通过 `asyncio.Task` + `asyncio.Event` 协作完成，不强制杀线程。

## 8. 扩展建议

- **安卓端**：协议层需要独立规范与测试向量；UI 用原生实现，复用协议而不是复用 PySide6 代码。
- **远控**：媒体采集/编码与输入注入是另一套技术栈，建议独立产品/仓库；本引擎只提供身份、信任和连接。
- **中继**：会改变「无服务器」定位，需要单独的信号与中继服务，并重新评估信任模型。
