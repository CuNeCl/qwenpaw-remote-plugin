# Remote SSH

Remote SSH 是一个 QwenPaw 插件，用于给当前聊天会话建立 SSH 连接。连接生效后，同一会话里的 shell 命令会被转发到远端机器执行。远端既可以是 POSIX 系统（Linux / macOS / BSD），也可以是 Windows。

## 兼容性

- 需要 **QwenPaw 2.0.0 及以上**（插件使用 `register_middleware` / `register_control_command` / `register_http_router`）。
- 不兼容 QwenPaw 1.x：旧版依赖的 monkey-patching 路径已移除。
- 验证过的宿主版本范围见 `plugin.json` 的 `qwenpaw_version`。

## 功能

- `remote_connect`：使用密码或 SSH 私钥连接远端机器。
- `remote_reconnect`：使用缓存的连接参数重连远端机器。
- `remote_disconnect`：断开当前会话的 SSH 连接。
- `remote_list`：查看当前会话的 SSH 连接状态。
- `remote_exec`：显式在远端机器执行一条命令。
- `remote_info`：查看远端设备信息（系统、架构、CPU、内存、磁盘、已安装工具等），支持缓存和强制刷新。
- `remote_health`：检查当前 SSH 连接的健康状态（延迟、失败次数、重连可用性）。
- `remote_set_cwd`：设置当前会话的默认远程工作目录，后续所有命令将在该目录执行。
- `remote_sudo`：使用 sudo 权限在远端机器执行命令，支持非交互式密码认证（仅 POSIX 远端）。
- `/remote`：在对话中管理当前会话的 SSH 连接，支持连接、断开、切换目录、sudo 执行等操作。
- 透明 shell 转发：连接成功后，同一会话里的 `execute_shell_command` 会通过 SSH 在远端执行。
- 前端管理页：在 Remote SSH 页面保存多个连接选项、跳板机等配置，并用每个选项上的开关控制当前 SSH 连接。
- 跳板机：连接选项可以选择已保存的 jump host，也可以通过命令行参数临时指定跳板机。
- Web 状态栏：在 QwenPaw Web 端 header 显示当前会话的 SSH 状态，并可快速连接或断开已保存设备。
- 连接健康监控：自动心跳检测连接状态，断线后可一键重连。
- Profile 测试连接：保存或使用连接配置前，可验证 SSH 参数是否正确。
- POSIX / Windows 双平台：自动探测远端系统类型与登录 shell，并按平台生成对应的命令包装、工作目录切换与环境探测脚本。
- 中英文双语界面：跟随宿主语言设置自动切换。

> 工具默认处于未启用状态。首次使用前请在 Agent 工具管理中启用所需的 `remote_*` 工具，否则模型无法调用它们。

## 安全模型

- **主机密钥校验（TOFU）**：默认加载系统 / 用户 `known_hosts` 与插件自己的 `WORKING_DIR/remote/known_hosts`，并用 `RejectPolicy` 拒绝未知主机密钥，避免中间人攻击。首次连接新机器时，连接会失败并给出提示；此时前端会弹出「信任并重试」确认框（也可用 profile 的「信任未知主机密钥」开关、`remote_connect(accept_new_host_key=True)` 或 `/remote connect accept_new_host_key=true`）。**信任动作会一次性把该主机密钥写入插件的 `known_hosts`，因此后续连接无需再开开关，并且仍会严格校验**——如果之后主机密钥变化，连接会被拒绝并提示密钥不一致（可能是中间人攻击或主机重装），需要人工核对指纹后手动清理旧条目。
- **sudo 凭据独立**：sudo 密码来自 profile 中独立的 `sudo_password` 字段或 `POST /api/remote/connections/{session_id}/sudo`，**不会复用 SSH 登录密码**。
- **凭据存储**：`profiles.json` 位于 `WORKING_DIR/remote/`，SSH 密码与口令为明文存储并尽量设置为 `0600` 权限。备份、目录同步或容器快照会一并带走这些文件，请把它当作敏感文件对待；能使用密钥认证时优先使用密钥。
- **会话归属校验**：连接建立时会记录调用方主体（宿主注入的 `X-Agent-Id` / `X-User-Id`）。会话级接口会校验主体一致性，避免跨调用方复用他人的 SSH 连接。若宿主未提供身份头（单用户本地部署），该校验退化为放行。
- **连接失效不静默降级**：如果本会话曾经连接过远端但连接已失效，`execute_shell_command` 会被**阻断并提示重连**，不会静默在本机执行，以免破坏性命令跑错机器。
- **尝试限流**：`/connections`、`/profiles/{id}/connect`、`/profiles/*/test` 等接口对每个客户端有每分钟尝试次数上限。

## 前端页面

`New Connection` 只负责新增连接选项，保存后会持久化到 QwenPaw 工作目录。页面可以保存多个连接选项，但当前会话最多保持一个活跃 SSH 连接。

`New Jump Host` 用于保存跳板机配置。创建或编辑连接选项时，可以在 `Jump Host` 下拉框中选择一个已保存的跳板机；留空则表示直连目标设备。

每个连接选项右侧都有连接开关：

- 打开开关：连接到这台设备；如果当前会话已经连接到其他设备，会自动切换到这台设备。
- 关闭开关：断开当前 SSH 连接。
- 删除按钮：删除这个连接选项；如果它正在连接，会先断开再删除。

连接状态下，连接选项下方会显示当前默认工作目录，并可通过编辑按钮修改（只更新该 profile 的工作目录字段，其它配置保持不变）。

编辑连接配置时，密码、密钥口令和 sudo 密码字段旁会显示"已设置"标记，提示该字段已有保存的值。留空提交则保留原有值。

Web 端 header 中的 SSH 状态按钮会显示当前会话是否已连接，并展示延迟和设备信息。连接断开时显示重连按钮。

## 对话命令

```text
/remote
```

查看当前会话的远程连接状态。

```text
/remote connect host=192.168.0.106 username=root password=你的密码
```

用密码连接远端机器。

```text
/remote connect root@192.168.0.106 key_path=C:\Users\me\.ssh\id_rsa
```

用 SSH 私钥连接远端机器。

```text
/remote connect root@10.0.0.12 jump_name=bastion
```

通过已保存的跳板机连接远端机器。

```text
/remote connect root@10.0.0.12 jump_host=bastion.example.com jump_username=root
```

通过临时指定的跳板机连接远端机器。

```text
/remote connect root@192.168.0.106 accept_new_host_key=true
```

信任首次连接的未知主机密钥（请先核对指纹）。

```text
/remote pwd
```

在远端机器执行 `pwd`。

```text
/remote 执行pwd
```

同样会在远端机器执行 `pwd`。

```text
/remote cd /workspace/app
```

切换当前会话的默认远程工作目录。

```text
/remote sudo systemctl restart nginx
```

使用 sudo 权限在远端机器执行命令。

```text
/remote disconnect
```

断开当前会话的 SSH 连接。

## 远端平台支持

连接建立时会自动探测远端平台（一次 `uname -s` 探测，Windows 上再加一次 `echo %COMSPEC%` 探测登录 shell），探测结果决定后续命令的语法：

| 远端平台 | 检测到的 shell | 工作目录切换 | 工作目录校验 | sudo |
| --- | --- | --- | --- | --- |
| POSIX | `$SHELL`（bash / zsh / dash / fish 等） | `cd <path> && cmd`（fish 使用 `; and`） | `pwd` | 支持 |
| Windows | `powershell` | `Set-Location -LiteralPath '<path>'; cmd` | `(Get-Location).Path` | 不支持 |
| Windows | `cmd` | `cd /d "<path>" && cmd` | `echo %CD%` | 不支持 |

- 工作目录以 `~` 开头时按 `$HOME` 展开（POSIX）。
- Windows 远端的环境探测由 PowerShell 或 cmd 脚本完成，因此部分字段（如内存）在 cmd 下可能为空。
- 在 Windows 远端执行 `remote_sudo` 会返回明确错误，请在远端自行以管理员身份执行。

## 安装

可以通过 QwenPaw 插件管理页面上传打包后的 ZIP 文件安装，也可以通过插件安装 API 传入 ZIP 路径安装。

安装包根目录必须包含 `plugin.json`。运行时 Python 依赖声明在 `requirements.txt` 中，QwenPaw 会在安装插件时自动安装这些依赖。

## 构建前端

安装包需要包含已构建的前端入口文件 `ui/dist/index.js`。

前端构建会先执行 `tsc --noEmit` 类型检查，再读取根目录 `plugin.json` 中的 `version`，并注入到 `ui/dist/index.js` 中作为插件初始化版本标记。不要在 `ui/src/index.ts` 中手写版本号。

```powershell
cd ui
npm ci
npm run build
```

仅做类型检查：

```powershell
cd ui
npm run typecheck
```

## 打包 ZIP

从仓库根目录执行：

Windows PowerShell：

```powershell
.\scripts\package.ps1
```

Linux / macOS：

```bash
chmod +x scripts/package.sh
./scripts/package.sh
```

脚本会自动读取 `plugin.json` 中的版本号，生成安装包 `dist\qwenpaw-remote-plugin-<version>.zip`。ZIP 根目录必须直接包含 `plugin.json`，不要多包一层仓库目录。打包脚本会使用跨平台的 ZIP 内部路径格式，避免在不同系统安装时找不到 `remote/` 后端包。

QwenPaw Web 端会按 `plugin.json` 的 `entry.frontend` 加载前端 bundle。`entry.frontend` 应设置为 `ui/dist/index.js`：

```json
{
	"version": "0.3.0",
	"entry": {
		"frontend": "ui/dist/index.js"
	}
}
```

打包脚本会校验这个一致性；如果 `entry.frontend` 被改成其他路径，打包会失败。

插件包内的 `plugin.json` 始终使用裸路径，不包含版本号。浏览器缓存由 **QwenPaw Web 宿主**负责（在加载插件前端时按插件版本追加 cache-busting 参数，例如把 `ui/dist/index.js` 加载为 `ui/dist/index.js?v=0.3.0`）。如果浏览器页面仍显示旧 UI，先完整刷新页面；必要时重启 QwenPaw Web 服务或清理浏览器缓存。

如果本机已经安装过前端依赖，可以跳过 `npm ci`：

```powershell
.\scripts\package.ps1 -SkipInstall
```

```bash
./scripts/package.sh --skip-install
```

## 自动打包发布

本项目使用 GitHub Actions 自动打包发布。当创建版本标签时，会自动触发 CI 流程：

### 发布步骤

1. 更新 `plugin.json` 中的版本号
2. 提交更改：`git commit -am "bump version to X.Y.Z"`
3. 创建标签：`git tag vX.Y.Z`
4. 推送标签：`git push origin vX.Y.Z`
5. GitHub Actions 会自动打包并创建 Release

### 手动打包

如果需要手动打包，可以运行：

```bash
./scripts/package.sh
```

打包后的文件在 `dist/` 目录中。

## 安装包内容

安装 ZIP 中只包含运行所需文件：

- `plugin.json`
- `plugin.py`
- `requirements.txt`
- `README.md`
- `LICENSE`
- `remote/`（含 `platform.py`、`ssh_manager.py`、`shell_wrapper.py`、`routers/`、`tools/` 等）
- `ui/dist/index.js`

不包含 `node_modules`、`__pycache__`、`.venv`、`.git`、测试目录或其他开发阶段生成文件。

## 测试

`tests/` 下是针对纯逻辑的回归测试（平台命令包装、profile 增量更新、存储原子写入等）：

```bash
python -m pytest tests -q
```

需要额外安装 `pytest`，它不在插件运行时依赖中。

## Docker 中安装

如果 QwenPaw 运行在 Docker 中，插件目录通常位于容器内的 `/app/working/plugins`。可以把打包后的 ZIP 复制进容器，再使用容器内的 QwenPaw CLI 强制安装：

```bash
docker cp dist/qwenpaw-remote-plugin-0.3.0.zip copaw:/tmp/qwenpaw-remote-plugin-0.3.0.zip
docker exec copaw sh -lc '/app/venv/bin/qwenpaw plugin install /tmp/qwenpaw-remote-plugin-0.3.0.zip --force'
```

安装后可以检查 Web 端暴露的插件清单：

```bash
curl -sS http://127.0.0.1:8088/api/frontend_plugin
```

返回内容中应包含当前版本号。

## REST API

插件接口挂载在 `/api/remote` 下：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/remote/connections` | 列出当前调用方的 SSH 连接（可用 `session_id` 过滤） |
| GET | `/api/remote/connections/active` | 按调用方主体解析当前连接，并返回其 `session_id`（管理页在设置路由下拿不到"当前会话"，靠这个接口） |
| POST | `/api/remote/connections` | 新建连接 |
| DELETE | `/api/remote/connections/{session_id}` | 断开连接 |
| POST | `/api/remote/connections/{session_id}/exec` | 在远端执行命令 |
| GET | `/api/remote/connections/{session_id}/status` | 连接状态 |
| GET/POST | `/api/remote/connections/{session_id}/health[/check]` | 健康状态 / 立即检查 |
| POST | `/api/remote/connections/{session_id}/reconnect` | 使用缓存参数重连 |
| GET/PUT | `/api/remote/connections/{session_id}/cwd` | 读取 / 设置会话默认工作目录 |
| GET | `/api/remote/connections/{session_id}/info` | 远端环境信息（`?refresh=true` 强制刷新） |
| POST | `/api/remote/connections/{session_id}/info/refresh` | 强制刷新环境信息 |
| POST/GET | `/api/remote/connections/{session_id}/sudo[/verify]` | 配置 / 查询 sudo |
| GET/POST | `/api/remote/profiles` | 列出 / 新建连接配置 |
| PUT/DELETE | `/api/remote/profiles/{profile_id}` | 更新 / 删除连接配置 |
| PATCH | `/api/remote/profiles/{profile_id}/cwd` | 仅更新该配置的默认工作目录 |
| POST | `/api/remote/profiles/{profile_id}/connect` | 用已保存配置连接；`session_id` 可为空（新建对话尚无会话 id）——此时返回 `{pending:true}`，并在本对话发出第一条消息时自动连接 |
| POST | `/api/remote/profiles/test`、`/api/remote/profiles/{profile_id}/test` | 测试连接 |
| GET/POST | `/api/remote/jump-hosts` | 列出 / 新建跳板机 |
| PUT/DELETE | `/api/remote/jump-hosts/{jump_host_id}` | 更新 / 删除跳板机 |

`profiles` 与 `jump-hosts` 属于宿主级配置，不与会话绑定；`connections/*` 属于会话级资源，会校验调用方主体。
