# AI Companion — Agent Core

当前实现独立于桌宠 UI：命令行会话、模型角色路由和有界工具调用循环。

Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
ollama pull qwen3:8b
.\.venv\Scripts\python.exe -m companion
```

Ollama is required for `/chat`. For `/coding`, configure the API Key as described below. `/chat` and `/coding` switch model roles. `/exit` exits.

可以输入“现在内存使用率是多少？”或“查看 CPU 和内存状态”。Agent 会把工具结果回填给模型，再显示最终回答。`/chat` 使用本地 Qwen3 8B；`/coding` 使用远程 OpenAI 模型，不需要本地 Ollama。

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

聊天和编程角色共享已完成的会话历史；切换 `/coding` 后，这段历史及需要的工具结果会发送给 OpenAI。Ollama 的内部 thinking 字段不会直接发送给 OpenAI。云端 coding 验收不需要先安装本地模型；本地 `/chat` 仍需 Ollama 和 Qwen3 8B。

Run tests:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
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

测试不依赖真实模型，覆盖工具执行、参数拒绝、超时、调用预算、失败时的会话历史、命令行、Ollama Python SDK 消息序列化／解析，以及 OpenAI Responses 请求、加密 reasoning 回填、工具调用 ID、鉴权错误和旧会话转换。集成测试使用模拟 HTTP 响应，不代表真实模型推理已通过。

本地聊天另需本机运行 `ollama serve` 并完成 `ollama pull qwen3:8b`。远程 coding 需要 API Key、模型访问权限和额度。请分别验收普通聊天／编码、CPU／内存查询、查询后的追问，以及服务不可用时的错误提示。Key 缺失时，`/coding` 请求会报告变量名称，仍可退出或切换回 `/chat`。

当前尚未包含流式输出、重试、多模型回退、持久化记忆、任务队列、GPU 管理或桌宠 UI。
