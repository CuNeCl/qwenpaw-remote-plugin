# Agent 指引 — QwenPaw Remote SSH 插件

QwenPaw 2.0+ 插件，为聊天会话提供透明的 SSH 远程执行能力。连接建立后，会话中所有 `execute_shell_command` 调用自动通过 paramiko 转发到远端机器（POSIX 或 Windows）。

**仅支持 QwenPaw 2.0+**：插件通过 `register_middleware` / `register_control_command` / `register_http_router` 扩展宿主，不使用 monkey-patching，不兼容 1.x。

## 快速参考

| 操作 | 命令 |
|------|------|
| 安装前端依赖 | `cd ui && npm ci` |
| 构建前端（含类型检查） | `cd ui && npm run build` |
| 仅类型检查 | `cd ui && npm run typecheck` |
| 开发模式（监听） | `cd ui && npm run dev` |
| 运行测试 | `python -m pytest tests -q` |
| 打包 ZIP | `./scripts/package.sh` |
| 打包（跳过 npm ci） | `./scripts/package.sh --skip-install` |

无 lint/格式化工具。

## 架构

```
plugin.py              → 插件系统后端入口 shim（导出 remote.plugin.plugin）
remote/plugin.py       → 后端实现入口（注册 tools、command、middleware、router、shutdown hook）
remote/context.py      → ContextVar，中间件用它把 session_id 传给 remote_* 工具
remote/platform.py     → POSIX / Windows 平台抽象：命令包装、路径引用、工作目录探测、环境脚本
remote/ssh_types.py    → SSH 数据结构、健康状态、环境快照
remote/ssh_manager.py  → 单例 SSH 连接管理器（paramiko）
remote/shell_wrapper.py → AgentScope MiddlewareBase：拦截 shell 命令 → SSH 执行
remote/store.py        → JSON 文件持久化（连接配置/跳板机，原子写入）
remote/routers/        → FastAPI REST API 和请求 schema（挂载在 /api/remote）
remote/tools/          → 工具实现（connect、disconnect、list、exec、/remote 命令）
ui/src/index.ts        → 单文件 React 前端（无 JSX，直接用 createElement）
```

## 核心模式

- **`PluginApi` 注册**：`register_middleware()` 注入 SSH 中间件，`register_control_command()` 注册 `/remote`，`register_http_router()` 挂载 `/api/remote`。控制命令的导入失败会被捕获并降级，不影响工具与中间件加载。
- **ContextVar 传递 session_id**：`remote/context.py` 持有 `ContextVar[str | None]`，由中间件在每次请求中 set/reset，调用栈深处的 remote_* 工具无需显式传参即可拿到当前 session ID。
- **异步生成器中间件**：`remote/shell_wrapper.py` 在检测到活跃 SSH 连接时拦截 `execute_shell_command`；若连接曾存在但已失效，则阻断执行并提示重连（不得静默回退本地）。
- **平台分支集中**：任何 shell 语法差异都必须放进 `remote/platform.py`（命令包装、路径引用、探测命令、环境脚本），`SSHManager` 不判断平台。
- **单例 `SSHManager`**：所有连接以 `session_id` 为键。负责跳板机隧道、主机密钥策略、健康检查、命令超时、环境探测缓存。
- **前端宿主 API**：React、ReactDOM、Ant Design 由宿主通过 `window.QwenPaw.host` 提供，不要直接 import。注册扩展只用文档化的 `window.QwenPaw.route/menu/slot/chat.*`。**请求不要用 `host.fetch`**：实测某些宿主版本会忽略 `options.method`，把 POST/PATCH 静默变成 GET（表现是写操作「成功」但实际未生效，或返回 405）。统一走 `apiFetch()`，它用 `host.getApiUrl(path)` + 原生 `fetch` 显式传 method，并手工补 `Authorization` 与 `X-Agent-Id` 头。
- **session id 解析**：`getSessionId()` 返回 `string | null`，按 `host.getCurrentSessionId()` → `window.currentSessionId`/`window.sessionId` → `chat.requestPayload` 回调缓存 → `/connections/active` 探测缓存的顺序探测。**不得再造共享兜底 id**（曾用 `"default-session"`，会导致跨会话串号）。轮询循环（5s/10s）静默使用，不做弹窗；只有用户主动操作才用 `requireSessionId(zh, sessionId)` 提示一次。
- **会话无关的接口不要绑 session**：`/profiles`、`/jump-hosts` 是宿主级配置，前端不带 `session_id` 也要能列出来。管理页与 header 状态一律先用 `GET /connections/active`（按 `X-Agent-Id` 解析，返回 `session_id` + `connection` + `health`），只有真正会话级的行为（断开、cwd、sudo）才用返回的 session_id。**原因**：设置路由下 `host.getCurrentSessionId()` / `useCurrentSession()` 正常返回 null（不是 API 缺失），前端无法自行得知会话。
- **延迟连接（deferred connect）**：新建对话在发出第一条消息前**没有 session id**，因此 `POST /profiles/{id}/connect` 允许 `session_id` 为空——此时按 `X-Agent-Id` 记入 `set_pending_connect` 并返回 `{pending: true}`，由中间件在首个聊天请求里 `await materialize_pending(session_id, ctx.agent_id)` 兑现（先 pop 再 await，避免并发重复连接）。管理页显示「待连接」Tag（`/connections/active` 的 `pending_profile_id`）。
- **session→agent 映射**：中间件在每次聊天请求里 `remember_session_owner(session_id, ctx.agent_id)`，因此聊天里通过 `/remote connect` 建立的连接（owner 为空）也能被同一 agent 的管理页找到；映射有 512 条上限并淘汰最旧项。

## 编码约定

- **Python**：3.10+ 语法（`str | None`、`from __future__ import annotations`）。异步优先，阻塞的 paramiko 调用用 `asyncio.to_thread()` 包装。
- **前端**：单文件 `ui/src/index.ts`，无 JSX，使用 `React.createElement()`。Vite library 模式打包为单个 ES module。React/ReactDOM 作为 peer deps 外部化。
- **打包**：以扁平 ZIP 分发，根目录必须直接包含 `plugin.json`。仅包含运行时文件（完整列表见 [README.md](README.md#安装包内容)）。
- **语言**：面向用户的字符串和文档使用中文，代码标识符使用英文。

## 插件系统契约

- `plugin.json` 声明元数据、`qwenpaw_version`、tools 和 commands
- `plugin.py` 导出 `plugin` 实例，提供 `register(api)` 方法
- 注册方式：`api.register_tool()`、`api.register_control_command()`、`api.register_middleware()`、`api.register_http_router()`、`api.register_shutdown_hook()`
- 工具返回 `qwenpaw` 的 `ToolResponse`；文本块使用 `agentscope` 的 `TextBlock`；控制命令返回 `agentscope` 的 `Msg`
- 控制命令处理器必须继承 `qwenpaw.runtime.commands.control.base.BaseControlCommandHandler`，实现 `command_name`（裸名，无斜杠）、`help_text` 与 `async handle(self, ctx, args)`

## 常见陷阱

- 前端 `ui/dist/` 不提交到仓库 — 修改 TypeScript 后必须重新构建（在 `ui/` 下执行 `npm run build`），打包脚本会把构建产物放进 ZIP。浏览器缓存由宿主负责，插件不再改写 `entry.frontend`。
- `SSHManager` 通过 `__new__` 实现单例 — 不要期望多次实例化能获得隔离。
- 跳板机连接会创建嵌套 transport 链；必须按正确顺序断开（由 `SSHManager.disconnect()` 管理）。
- `PUT /profiles/{id}` 是**全量替换**语义：局部字段修改必须走专用函数（如 `store.update_profile_cwd`），否则会把 host/username/key_path 重置为默认值。
- 未知主机密钥默认 `RejectPolicy` 拒绝。显式信任（profile 开关 / `remote_connect(accept_new_host_key=True)` / `/remote connect accept_new_host_key=true` / 前端「信任并重试」）会通过 `_remember_host_key` **一次性写入 `WORKING_DIR/remote/known_hosts`**（TOFU），之后连接照常严格校验；密钥不一致时抛 `BadHostKeyException` → `_host_key_mismatch_message`，**绝不允许自动信任**。`_load_known_hosts` 同时加载系统/用户/插件三处 known_hosts。
- sudo 密码来自 profile 的 `sudo_password` 或会话级配置接口，**不要**回退到 SSH 登录密码。Windows 远端不支持 sudo。
- 会话级 API 会校验调用方主体（`X-Agent-Id` / `X-User-Id`）；新增会话级路由时需调用 `_assert_session_owner`。
