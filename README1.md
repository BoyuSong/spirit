# AI Companion — Cloud Multi-Agent

当前实现独立于桌宠 UI：云端模型角色路由、有界工具循环，以及四个独立 Discord Bot 的任务流水线。默认配置全部使用云端模型。

Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install uv
.\.venv\Scripts\python.exe -m uv pip install --python .\.venv\Scripts\python.exe -r pyproject.toml --extra dev
$env:PYTHONPATH = "$PWD\src"
$env:PYTHONDONTWRITEBYTECODE = "1"
.\.venv\Scripts\python.exe -m companion
```

配置 API Key 后使用 `/chat`（协调）、`/coding`、`/review`、`/research` 切换云端模型。`/role NAME` 选择 `models.yaml` 中登记的其他模型配置键。`/exit` 退出。安装依赖时不使用 editable 模式，以保护已跟踪的 egg-info。

可以输入“现在内存使用率是多少？”或“查看 CPU 和内存状态”。Agent 会把工具结果回填给模型，再显示最终回答。模型和 Bot 的初始分工见 [Discord 多模型设计](docs/cloud-multi-agent.md)。

## 远程 coding 模型

`config/models.yaml` 中的 `coding` 已配置为 OpenAI Responses API：

```yaml
coding:
  provider: openai
  model: gpt-6.1-sol
  base_url: https://api.openai.com/v1
  api_key_env: CODING_API_KEY
```

`gpt-6.1-sol` 是 [OpenAI 官方 SDK](https://github.com/openai/openai-python/blob/main/src/openai/types/shared/chat_model.py) 列出的 GPT6.1 模型 ID；你的 API 账户是否可用仍需实际验证。可修改 `model` 为账户可用的其他 Responses API 模型。

在云环境设置中安全填写 `CODING_API_KEY` 并保存／发布相关配置。程序按 `api_key_env` 从环境读取凭据，仓库配置不存放 Key。本地 Windows 运行时，通过本机的凭据管理或进程环境提供同名变量。然后启动程序，输入 `/coding` 切换模型。

远程请求使用 `store: false`，工具 schema 转换为 Responses API 的 function 定义；保留工具调用 ID 和加密 reasoning 输出，以支持多轮工具调用。默认使用模型自身的采样参数，不发送可能被 reasoning 模型拒绝的 `temperature`。

命令行角色共享已完成的会话历史，调用时把可见历史及工具结果发送给对应云端模型。不同模型不会复用彼此的加密 reasoning；同一模型的工具回填保留其加密 reasoning。

## Discord 多角色协作

`config/agents.yaml` 登记角色、模型配置键、独立 Bot Token 变量名和流程。默认流程为“协调拆解 → 研究 → 编码 → 审查 → 协调汇总”。模型选择可在 `config/models.yaml` 修改。

需要四个 Bot Token、`DISCORD_CHANNEL_ID` 和 API Key。在 Discord 配置频道权限及协调 Bot 的 Message Content Intent 后，主动运行：

```powershell
.\.venv\Scripts\python.exe -m companion.discord_workflow "设计一个带测试的配置加载器"
```

该命令会向所选频道发送任务和结果，调用云端模型。参数 `--help` 只显示帮助。配置步骤、预算、消息过滤和后续新增模型／Bot 的方式详见 [Discord 多模型设计](docs/cloud-multi-agent.md)。

Run tests:

```powershell
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
```

## Agent 行为

- 唯一内置工具为 `get_system_status`，读取运行 Agent 的机器的 CPU 和内存状态，无参数。它查询的是实际运行机器；在云环境运行时不会读取你的 Windows 电脑。
- CPU 使用率采样窗口为 0.1 秒，内存字段包含总量、可用量、已用量（字节）和使用率。
- 只执行显式注册的工具；参数按 Pydantic schema 严格校验，拒绝未知字段和类型转换。
- 默认每次用户请求最多 3 轮工具调用、累计 8 次调用；同一响应中的多个调用按顺序执行。超过次数限制时停止该轮对话。
- 模型每次响应等待最多 120 秒，工具等待最多 5 秒。工具失败、超时或参数错误作为结构化结果回填，允许模型解释或在预算内修正。
- 只有得到非空的最终回答后，才更新会话历史。模型失败、空回答或超限时保留此前完成的会话；这不会撤销已经执行的工具，因此当前仅提供只读工具。
- 系统采样在线程中运行，避免阻塞异步调用；工具超时取消等待，不保证终止已经开始的线程。

新增工具时，使用 `ToolArguments` 声明参数，并在 `ToolRegistry` 注册异步处理函数。当前没有提供写文件、执行 shell 或修改系统的工具。

## 云环境／Linux 开发

在仓库根目录运行。环境安装脚本使用外部虚拟环境，避免 editable 安装改写仓库里已跟踪的 egg-info：

```bash
python3 -m venv /workspace/.venvs/spirit
UV_CACHE_DIR=/workspace/.cache/uv uv pip install --python /workspace/.venvs/spirit/bin/python -r pyproject.toml --extra dev
export PYTHONPATH="$PWD/src"
export PYTHONDONTWRITEBYTECODE=1
/workspace/.venvs/spirit/bin/python -m pytest -q -p no:cacheprovider
/workspace/.venvs/spirit/bin/python -m companion
```

测试不依赖真实模型，覆盖工具循环、超时、预算、会话历史、跨模型 reasoning 转换、OpenAI Responses、四 Bot 的 Discord HTTP 交接、新角色配置、长附件、消息过滤、重复消息和限流。集成测试使用模拟 HTTP 响应，不代表真实模型推理或 Discord 联调已通过。

真实运行需要 API Key、对应模型访问权限及额度。Discord 工作流另需独立 Bot Token、频道 ID 和权限。请验收云端普通聊天、角色切换、工具查询以及频道中的完整任务交接。Key 缺失时会报告变量名称，Discord 凭据缺失时不会发送任务。

当前为有界串行工作流；尚未包含流式输出、模型自动重试／回退、持久化记忆／队列、并行 DAG、实际文件修改／代码执行、联网检索或桌宠 UI。
