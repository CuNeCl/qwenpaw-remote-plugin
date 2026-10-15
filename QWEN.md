# QWEN.md - Remote SSH Plugin

## Project Overview

Remote SSH 是一个 **QwenPaw 插件**，为聊天会话提供 SSH 远程连接能力。连接建立后，同一会话内的所有 shell 命令会通过 SSH 透明转发到远端机器执行。支持 POSIX 与 Windows 远端。

### Key Technologies

- **Backend:** Python 3.10+, FastAPI, paramiko (SSH 客户端), agentscope SDK
- **Frontend:** React 18, TypeScript, Vite
- **Plugin Host:** QwenPaw 2.0.0+（不兼容 1.x）

### Architecture

插件完全通过 QwenPaw 2.0+ 的 `PluginApi` 扩展宿主能力，不使用 monkey-patching：

```
plugin.py (根入口 shim，负责在隔离加载环境下定位后端包)
└── remote/plugin.py (后端实现入口)
    ├── register_middleware()   → remote/shell_wrapper.py (SSH 拦截中间件)
    ├── register_control_command() → remote/tools/remote_command.py (/remote)
    ├── register_http_router()  → remote/routers/connections.py (REST API)
    └── register_shutdown_hook() → 关闭全部 SSH 连接

remote/ssh_manager.py (SSH 连接单例，按 session_id 管理；心跳、健康检查、跳板机、sudo、环境探测)
remote/platform.py (POSIX / Windows 平台抽象：语法族分类、命令包装、路径引用、探测与环境脚本)
remote/ssh_types.py (SSH 数据结构与健康状态)
remote/context.py (ContextVar：中间件向 remote_* 工具传递 session_id)
remote/store.py (持久化连接配置/跳板机到 profiles.json，原子写入)
remote/tools/ (remote_connect, remote_disconnect, remote_list, remote_exec, ..., RemoteCommandHandler)
ui/src/index.ts (单文件 React 前端，通过 window.QwenPaw.* 注册路由/菜单/插槽/工具渲染)
```

### Core Components

| File | Purpose |
|------|---------|
| `plugin.py` | 插件系统入口 shim，导出 `remote.plugin.plugin` |
| `remote/plugin.py` | 后端实现入口，注册 tools / command / middleware / router / shutdown hook |
| `remote/ssh_manager.py` | SSH 连接管理器单例，支持密码/密钥认证及跳板机，基于 paramiko |
| `remote/platform.py` | 远端平台抽象：shell 语法族分类、命令包装、路径引用、工作目录探测、环境探测脚本 |
| `remote/ssh_types.py` | SSH 连接数据结构、健康状态、环境快照 |
| `remote/context.py` | ContextVar，用于把中间件解析出的 session_id 传给 remote_* 工具 |
| `remote/shell_wrapper.py` | AgentScope MiddlewareBase，拦截 `execute_shell_command` 并转发到 SSH |
| `remote/store.py` | 连接配置/跳板机的持久化存储（JSON 文件，原子写入） |
| `remote/routers/connections.py` | FastAPI 路由，提供 REST API 管理 SSH 连接 |
| `remote/tools/` | 注册到 QwenPaw 的 tool 函数与 `/remote` 控制命令 |
| `ui/` | React 前端管理页面与 header 状态指示器 |

### Features

- 密码 / SSH 私钥认证，主机密钥默认严格校验（TOFU：显式信任后写入插件 known_hosts 持久生效）
- 跳板机（Jump Host）支持
- 连接配置持久化（Profiles），sudo 密码独立于 SSH 密码
- 透明 shell 命令转发；连接失效时阻断本地执行
- POSIX / Windows 远端自动探测与平台适配
- 远端 shell 可指定（profile 的 `default_shell` / 连接的 `shell`）：留空自动探测（`$SHELL` → 平台默认），非空时由 `classify_shell_grammar()` 分类出 posix / powershell / cmd 语法并跳过 Windows 的 `%COMSPEC%` 探测
- `/remote` 对话命令
- 前端连接管理页面与 header 状态浮层
- 会话归属校验（基于宿主注入的身份头）；`/connections/active` 按调用方主体解析活跃连接
- 新对话延迟连接：发出第一条消息前点击连接会记为 pending，首条消息时自动兑现

### Security Behaviors

- 未知主机密钥默认被 `RejectPolicy` 拒绝。显式信任（profile 开关 / `accept_new_host_key` / 前端「信任并重试」）会一次性写入 `WORKING_DIR/remote/known_hosts`（TOFU），之后仍严格校验；密钥不一致时拒绝连接并提示人工核对，绝不自动信任。`known_hosts` 同时加载系统/用户/插件三处。
- `profiles.json` 明文保存密码/口令，写入时尽量设置 `0600`。
- 会话级接口校验调用方主体；无身份头时退化为单用户放行。
- 连接失效且存在缓存参数时，`execute_shell_command` 被阻断而非回退本地。
- `/connections`、`/profiles/*/connect`、`/profiles/*/test` 有每客户端尝试限流。

## Building and Running

### Frontend

```powershell
cd ui
npm ci
npm run build
```

`npm run build` 会先执行 `tsc --noEmit` 类型检查，构建产物输出到 `ui/dist/index.js`，这是插件安装所需的入口文件。

### Packaging

Windows:
```powershell
.\scripts\package.ps1
```

Linux / macOS:
```bash
chmod +x scripts/package.sh
./scripts/package.sh
```

生成 `dist\qwenpaw-remote-plugin-<version>.zip`。ZIP 根目录必须包含 `plugin.json`。

跳过前端依赖安装：
```powershell
.\scripts\package.ps1 -SkipInstall
```

### Installation

通过 QwenPaw 插件管理页面上传 ZIP 安装包，或调用插件安装 API。QwenPaw 会自动安装 `requirements.txt` 中的 Python 依赖。

### Tests

```bash
python -m pytest tests -q
```

`tests/` 只覆盖纯逻辑（平台命令包装与探测、profile 增量更新、存储原子写入、REST 路由解析、主机密钥信任、待连接与活跃连接解析），需要额外安装 `pytest`。

## Development Conventions

- **Python 类型注解：** 全面使用类型提示，`from __future__ import annotations` 用于延迟求值
- **异步优先：** SSH 操作通过 `asyncio.to_thread` 包装阻塞调用，所有 API 路由为 async
- **平台分支集中：** 任何 shell 语法差异都放进 `remote/platform.py`，`SSHManager` 不判断平台
- **日志前缀：** 日志使用 `[Remote]` 前缀
- **敏感字段处理：** 响应中必须移除 `password` / `passphrase` / `sudo_password`（`_sanitize_secret_fields`）
- **配置持久化：** 存储到 `WORKING_DIR/remote/profiles.json`，原子写入；更新时保留未提供的敏感字段
- **profile 字段白名单：** `store._normalize_profile_payload` 逐字段构造 profile，新增字段（如 `default_shell`）必须显式加入，否则提交的值会被静默丢弃
- **profile 更新语义：** `PUT /profiles/{id}` 是全量替换；局部修改必须走专用函数（如 `update_profile_cwd`），不要把增量 payload 交给 `update_profile`
- **前端请求约定：** 不要用 `host.fetch`（部分宿主版本会忽略 `options.method`，把 POST/PATCH 静默变成 GET）；统一走 `apiFetch()`，用 `host.getApiUrl()` + 原生 `fetch` 显式传 method，并补 `Authorization` / `X-Agent-Id` 头
- **session id 约定：** `getSessionId()` 返回 `string | null`，按宿主 API → 全局变量 → `chat.requestPayload` 回调缓存 → `/connections/active` 探测的顺序解析；不得使用共享兜底 id（会造成跨会话串号）。轮询路径静默处理，只有用户主动操作才提示
- **会话级路由：** 新增会话级路由必须调用 `_assert_session_owner` 校验调用方主体
