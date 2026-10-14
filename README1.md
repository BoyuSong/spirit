# AI Companion — Cloud Functional Agents

默认使用云端模型，启用主管、Coding、网站三个功能 Bot。系统功能已并入 Coding，第四个 Agent 用途待定，不配置模型或 Token。主管使用你选的 DeepSeek `deepseek-flash`，开启思考模式、推理力度 `high`；Coding 与网站均使用 OpenAI GPT6.1（`gpt-6.1-sol`）和 `CODING_API_KEY`，分别持有自己的工具与会话。你可以直接调度，也可以让主管按需分派；每个启用角色使用独立 Discord Bot，不需要安装或启动本地模型。

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

在环境设置安全提供主管的 `DEEPSEEK_API_KEY` 和 OpenAI 的 `CODING_API_KEY`。直接调用 Coding 只需 OpenAI Key。交互入口使用 `/chat`、`/coding`、`/web`、`/role NAME`、`/exit`；角色分别保存已完成的会话历史。旧 `/system`、`/role system` 和 `--agent system` 兼容转到 Coding，共用 Coding 的工具和历史；Discord 工作流的 `--agent system` 同样转到 Coding。

本地运行时，在项目根目录（与 `pyproject.toml` 同级）复制并编辑 `.env`：

```powershell
# 仅首次创建；已有 .env 时直接编辑，保留原值。
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

在文件中填写自己的 API Key、Bot Token 和频道/用户 ID，再启动程序。命令行、Discord 工作流和监听器都会自动加载项目根目录的 `.env`，不搜索其他目录。已设置的进程/云环境变量优先；文件中的值按字面量读取，不展开 `${变量}`。`.env` 已被 Git 忽略，`.env.example` 只包含空值和非秘密示例。云环境直接在环境设置填写凭据即可，不必创建 `.env`。

也可直接运行单个功能：
```powershell
.\.venv\Scripts\python.exe -m companion --agent coding "写一个配置加载器"
.\.venv\Scripts\python.exe -m companion --agent coding "查看 CPU 和内存状态"
```

Coding 可读取项目、修改各类代码文件、按哈希校验做局部编辑，并调用本机运行时或编译器运行、构建和测试；同时具备系统状态、目录和文件读取，以及下载、回收删除和恢复工具。网站具备联网搜索、普通网页读取、浏览器渲染和目标字段提取。Coding 按你的当前选择自动执行，无需逐次批准，操作仍有日志。

在 `.env` 中配置 `CODING_WORKSPACE` 指向目标项目。默认 `.coding-workspace` 是独立演示目录；`SYSTEM_WORKSPACE` 默认 `.spirit-workspace`。例如 `CODING_WORKSPACE=D:/works/目标项目`。pytest 使用运行 Bot 的 Python 环境，目标项目依赖需要安装到该环境。文件工具约束工作区路径；执行测试仍以当前用户权限运行项目代码，不是操作系统沙箱。

其他语言无需更换模型或新增语言 API。`run_project_command` 接受程序和参数数组，例如 `['node', 'example.js']`、`['npm', 'test']`、`['go', 'test', './...']`、`['javac', 'Main.java']`，工作目录必须位于 `CODING_WORKSPACE`。编译型语言可先编译、再运行产物；TypeScript 按项目已有脚本运行或测试。先在本机安装对应运行时、编译器和项目依赖，确保程序在 `PATH` 中，然后重启 Bot。Python 命令使用 Bot 自己的虚拟环境。

项目命令默认 30 秒、最多 60 秒，返回实际退出码及最多 64 KiB 输出；长驻服务会超时。命令参数不进行 shell 字符串解析，不能用一条 `&&` 字符串拼接多步操作。Windows 的 npm/npx 使用 Node 执行其 JS 入口；其他 `.bat`/`.cmd` 包装器需改用原生程序或脚本解释器。命令成功不代表测试实际执行，Bot 必须结合输出说明验证结果。

## 网站搜索与信息提取

后续方案拟采用用户提到的 ego lite 和 ds v41；项目链接、服务地址及准确 API 型号尚待确认，因此目前保留 GPT6.1 配置和已验证的 Playwright 工具。若切换 DeepSeek，需把 OpenAI 原生搜索替换为独立搜索工具，不能仅修改模型名称。

网站 Bot 使用 OpenAI 原生 `web_search` 找来源，`read_webpage` 读取普通网页；需要 JavaScript 时用 Playwright/Chromium 的 `browse_webpage`，再按用户目标用 `extract_webpage` 提取正文、标题、价格、作者、时间或链接等字段。字段未匹配会明确返回未找到，结果附实际来源 URL。网页内容仅作为资料，不作为执行指令。

本机安装浏览器扩展（原生搜索和普通网页读取不需要 Chromium）：

```powershell
.\.venv\Scripts\python.exe -m uv pip install --python .\.venv\Scripts\python.exe -r pyproject.toml --extra dev --extra browser
.\.venv\Scripts\python.exe -m playwright install chromium
```

默认优先使用 `WEB_BROWSER_EXECUTABLE_PATH`，然后查找 `PATH` 中的 Chromium/Chrome，否则使用 Playwright 下载的 Chromium。`.env` 中的路径变量可保持空白。修改安装或配置后重启 Bot，例如：`@网站Bot 搜索 Python 配置读取的官方文档，访问来源并提取代码示例和链接`。

直接访问的页面及其脚本、API、重定向目标都必须在 `config/agents.yaml` 的 `web_domains` 内，新增目标网站时补上准确域名并重启；云端还需允许相应网络域名。原生搜索由 OpenAI 服务端访问网络，可搜索域名表外的来源。模型是否支持原生搜索及账号访问需真实 API 验证；浏览器工具本身不会替代搜索服务。

浏览器首版只读，支持 GET/HEAD、JavaScript 渲染、链接和分页 URL 读取；每次调用是独立会话，不保留 Cookie，不提供登录、表单提交或点击操作。提取工具最多 8 个字段，每字段最多 3 个匹配、1000 字符，支持指定 `href`/`datetime`/`content` 等属性；缺失或截断会标记。正文沿用读取分页限制；动态页面重新读取时内容可能变化，分页偏移不保证跨调用快照一致。

## Discord 调度

主管工作流和完整监听器需两家模型的 API Key、三个独立 Bot 的 Token、操作频道 `DISCORD_CHANNEL_ID` 和独立通信频道 `DISCORD_BUS_CHANNEL_ID`。新建一个普通文字频道供 Bot 交接任务，三个 Bot 在两个频道均需 View Channel、Read Message History、Send Messages、Attach Files；主管需 Message Content Intent。旧 `DISCORD_SYSTEM_BOT_TOKEN` 不再使用，保留在本地 `.env` 不影响启动。按 [配置与权限说明](docs/cloud-multi-agent.md) 设置。

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

Coding 的系统文件工具使用 `SYSTEM_WORKSPACE`，云环境建议 `/workspace/spirit-runtime/system`；代码工具使用 `CODING_WORKSPACE`，建议 `/workspace/spirit-runtime/coding`。两个变量继续有效，由同一个 Bot 根据工具区分使用。删除移入应用回收区，返回 `restore_id`，可让 Coding 恢复；下载和恢复不覆盖现有目标。Discord 的系统任务也直接 @Coding，旧系统 Bot 不再由当前程序运行。云端运行时操作云端机器。

执行策略由 `config/agents.yaml` 的角色 `approval_mode` 控制，目前 Coding 为 `automatic`，覆盖编码和系统操作。需要逐次批准时改为 `manual` 并重启：Discord 回复 `@Coding approve 请求ID` 或 `deny 请求ID`，命令行在终端确认。模型不能自行改变后端策略。

完整的 [功能、配置、授权、扩展与限制](docs/cloud-multi-agent.md) 包含全部环境变量、网站域名、授权超时和回收恢复方式。API Key、Token 不写入仓库或聊天。

## 云环境开发与验证

依赖安装到外部虚拟环境，避免 editable 安装改写已跟踪的 egg-info：

```bash
python3 -m venv /workspace/.venvs/spirit
UV_CACHE_DIR=/workspace/.cache/uv uv pip install --python /workspace/.venvs/spirit/bin/python -r pyproject.toml --extra dev --extra browser
export PYTHONPATH="$PWD/src"
export PYTHONDONTWRITEBYTECODE=1
/workspace/.venvs/spirit/bin/python -m pytest -q -p no:cacheprovider
```

测试使用模拟 DeepSeek/OpenAI/Discord HTTP 和临时文件，验证跨服务调度、工具隔离、批准等待、文件操作及消息边界。浏览器测试用真实 Chromium 渲染本地拦截返回的 HTML/JavaScript，不访问真实网站；无 Playwright 或系统 Chromium 时跳过这部分测试。真实模型访问、联网搜索和 Discord 联调仍需有效凭据、账户权限及频道权限；保存云环境草稿不会注入凭据或自动发布配置。

当前 job 保存在内存中，重启不恢复中断任务。操作日志在系统工作区的 `.agent-internal/audit.jsonl`。文件操作完成后不会因后续模型失败自动回滚，需查看日志和文件状态。尚未包含持久化队列、独立 worker、交互终端、浏览器登录/表单操作或桌宠 UI。
