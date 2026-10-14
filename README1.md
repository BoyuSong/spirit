# AI Companion — Cloud Functional Agents

默认使用云端模型：主管、Coding、网站、系统四个功能 Bot。主管先用 DeepSeek `deepseek-chat`，编码使用 OpenAI GPT6.1（`gpt-6.1-sol`）；系统和网站暂保留 OpenAI 占位配置，型号未定。你可以直接调度，也可以让主管按需分派；每个角色使用独立 Discord Bot，不需要安装或启动本地模型。

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

在环境设置安全提供主管的 `DEEPSEEK_API_KEY` 和 OpenAI 的 `CODING_API_KEY`。直接调用 Coding 只需 OpenAI Key。交互入口使用 `/chat`、`/coding`、`/web`、`/system`、`/role NAME`、`/exit`；角色分别保存已完成的会话历史。也可直接运行单个功能：

```powershell
.\.venv\Scripts\python.exe -m companion --agent coding "写一个配置加载器"
.\.venv\Scripts\python.exe -m companion --agent system "查看 CPU 和内存状态"
```

编码当前生成和检查代码，不执行代码或改写源文件；网站具备联网搜索和公开网页读取；系统具备状态、目录和文件读取，以及逐次批准后的下载和删除。

## Discord 调度

主管工作流和完整监听器需两家模型的 API Key、四个独立 Bot 的 Token、频道 `DISCORD_CHANNEL_ID`，按 [配置与权限说明](docs/cloud-multi-agent.md) 添加 Bot 并开启 Message Content Intent。

```powershell
# 主管按需分派
.\.venv\Scripts\python.exe -m companion.discord_workflow "查资料并设计一个配置加载器"
# 直接调度指定功能 Bot
.\.venv\Scripts\python.exe -m companion.discord_workflow --agent coding "写一个配置加载器"
# 持续接收 @Bot 调度和批准消息，另需 DISCORD_OPERATOR_IDS
.\.venv\Scripts\python.exe -m companion.discord_listener
```

监听器启动后，你可在配置频道 `@功能Bot 任务`，或 `@主管 任务`。命令会发送 Discord 消息，不会自动开机启动。主管无需调用全部角色；新增角色通过模型与 Agent 配置注册。

系统文件限定在 `SYSTEM_WORKSPACE`，默认 `.spirit-workspace`，云环境建议使用 `/workspace/spirit-runtime/system`。下载、删除必须由发起用户逐次批准；Discord 回复 `@system approve 请求ID` 或 `@system deny 请求ID`，命令行在终端确认。删除移入应用回收区，下载不覆盖现有文件。设置只限定工作区，不会读取云端之外你的 Windows 电脑。

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

当前 job 保存在内存中，重启不恢复中断任务。文件操作完成后不会因后续模型失败自动回滚，需查看审计和文件状态。尚未包含持久化队列、独立 worker、任意 shell、源码写入/执行、浏览器自动化或桌宠 UI。
