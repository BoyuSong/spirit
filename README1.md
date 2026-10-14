# AI Companion — Cloud Functional Agents

默认使用云端模型：主管、Coding、网站、系统四个功能 Bot。主管使用你选的 DeepSeek `deepseek-flash`，开启思考模式、推理力度 `high`；编码使用 OpenAI GPT6.1（`gpt-6.1-sol`）。系统和网站暂保留 OpenAI 占位配置，型号未定。你可以直接调度，也可以让主管按需分派；每个角色使用独立 Discord Bot，不需要安装或启动本地模型。

## 安装与命令行

Windows PowerShell：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install uv
.\.venv\Scripts\python.exe -m uv pip install --python .\.venv\Scripts\python.exe -r pyproject.toml --extra dev
$env:PYTHONPATH = "$PWD\src"
$env:PYTHONDONTWRITEBYTECODE = "1"
.\.venv\Scripts\python.exe -m companion
```

在环境设置安全提供主管的 `DEEPSEEK_API_KEY` 和 OpenAI 的 `CODING_API_KEY`。直接调用 Coding 只需 OpenAI Key。交互入口使用 `/chat`、`/coding`、`/web`、`/system`、`/role NAME`、`/exit`；角色分别保存已完成的会话历史。

本地运行时，在项目根目录（与 `pyproject.toml` 同级）复制并编辑 `.env`：

```powershell
# 仅首次创建；已有 .env 时直接编辑，保留原值。
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

在文件中填写自己的 API Key、Bot Token 和频道/用户 ID，再启动程序。命令行、Discord 工作流和监听器都会自动加载项目根目录的 `.env`，不搜索其他目录。已设置的进程/云环境变量优先；文件中的值按字面量读取，不展开 `${变量}`。`.env` 已被 Git 忽略，`.env.example` 只包含空值和非秘密示例。云环境直接在环境设置填写凭据即可，不必创建 `.env`。

也可直接运行单个功能：
```powershell
.\.venv\Scripts\python.exe -m companion --agent coding "写一个配置加载器"
.\.venv\Scripts\python.exe -m companion --agent system "查看 CPU 和内存状态"
```

编码可读取项目、修改文件、按哈希校验做局部编辑，并运行真实 pytest；网站具备联网搜索、网页标题/链接和分页读取；系统具备状态、目录和文件读取，以及下载、回收删除和恢复。Coding 和系统按你的当前选择自动执行，无需逐次批准，操作仍有工作区范围和日志。

在 `.env` 中配置 `CODING_WORKSPACE` 指向目标项目。默认 `.coding-workspace` 是独立演示目录；`SYSTEM_WORKSPACE` 默认 `.spirit-workspace`。例如 `CODING_WORKSPACE=D:/works/目标项目`。pytest 使用运行 Bot 的 Python 环境，目标项目依赖需要安装到该环境。文件工具约束工作区路径；执行测试仍以当前用户权限运行项目代码，不是操作系统沙箱。

## Discord 调度

主管工作流和完整监听器需两家模型的 API Key、四个独立 Bot 的 Token、操作频道 `DISCORD_CHANNEL_ID` 和独立通信频道 `DISCORD_BUS_CHANNEL_ID`。新建一个普通文字频道供 Bot 交接任务，四个 Bot 在两个频道均需 View Channel、Read Message History、Send Messages、Attach Files；主管需 Message Content Intent。按 [配置与权限说明](docs/cloud-multi-agent.md) 设置。

```powershell
# 主管按需分派
.\.venv\Scripts\python.exe -m companion.discord_workflow "查资料并设计一个配置加载器"
# 直接调度指定功能 Bot
.\.venv\Scripts\python.exe -m companion.discord_workflow --agent coding "写一个配置加载器"
# 持续接收 @Bot 调度和批准消息，另需 DISCORD_OPERATOR_IDS
.\.venv\Scripts\python.exe -m companion.discord_listener
```

监听器启动后，你可在配置频道 `@功能Bot 任务`，或 `@主管 任务`。命令会发送 Discord 消息，不会自动开机启动。主管无需调用全部角色；新增角色通过模型与 Agent 配置注册。

内部协议通过通信频道的附件传输，不再将 JSON 正文发送到聊天频道。操作频道显示最终回答和运行错误；切回手动模式时，批准请求也在操作频道显示。两个频道必须不同，Bot 需要能访问两者。

Windows 可直接使用 `scripts/run-discord.bat` 启动监听器，`scripts/run-cli.bat` 启动命令行。这些脚本定位项目根目录并在退出时保留窗口，不需要把 PowerShell 语法写入 `.bat`。

系统文件限定在 `SYSTEM_WORKSPACE`，云环境建议 `/workspace/spirit-runtime/system`；Coding 建议 `/workspace/spirit-runtime/coding`。删除移入应用回收区，返回 `restore_id`，可让系统 Bot 恢复；下载和恢复不覆盖现有目标。云端运行时操作云端机器。

执行策略由 `config/agents.yaml` 的角色 `approval_mode` 控制，目前 Coding、system 为 `automatic`。需要逐次批准时改为 `manual` 并重启：Discord 回复 `@功能Bot approve 请求ID` 或 `deny 请求ID`，命令行在终端确认。模型不能自行改变后端策略。

完整的 [功能、配置、授权、扩展与限制](docs/cloud-multi-agent.md) 包含全部环境变量、网站域名、授权超时和回收恢复方式。API Key、Token 不写入仓库或聊天。

## 云环境开发与验证

依赖安装到外部虚拟环境，避免 editable 安装改写已跟踪的 egg-info：

```bash
python3 -m venv /workspace/.venvs/spirit
UV_CACHE_DIR=/workspace/.cache/uv uv pip install --python /workspace/.venvs/spirit/bin/python -r pyproject.toml --extra dev
export PYTHONPATH="$PWD/src"
export PYTHONDONTWRITEBYTECODE=1
/workspace/.venvs/spirit/bin/python -m pytest -q -p no:cacheprovider
```

测试使用模拟 DeepSeek/OpenAI/Discord HTTP 和临时文件，验证跨服务调度、工具隔离、批准等待、文件操作及消息边界。真实模型访问、联网搜索和 Discord 联调仍需有效凭据、账户权限及频道权限；保存云环境草稿不会注入凭据或自动发布配置。

当前 job 保存在内存中，重启不恢复中断任务。操作日志在系统工作区的 `.agent-internal/audit.jsonl`。文件操作完成后不会因后续模型失败自动回滚，需查看日志和文件状态。尚未包含持久化队列、独立 worker、任意 shell、浏览器自动化或桌宠 UI。
