# AI Companion — Agent Core

当前实现独立于桌宠 UI：命令行会话、模型角色路由和有界工具调用循环。

Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
ollama pull qwen3:8b
.\.venv\Scripts\python.exe -m companion
```

Ensure Ollama is running. `/chat` and `/coding` switch model roles. `/exit` exits.

可以输入“现在内存使用率是多少？”或“查看 CPU 和内存状态”。模型需要支持 Ollama 原生工具调用；默认使用 Qwen3 8B。Agent 会把工具结果回填给模型，再显示最终回答。

Change `config/models.yaml` to use a different local Ollama model, after pulling it. Both roles initially use Qwen3 8B.

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

测试不依赖真实模型，覆盖工具执行、参数拒绝、超时、调用预算、失败时的会话历史、命令行以及真实 Ollama Python SDK 的消息序列化／解析。SDK 集成测试使用模拟 HTTP 响应，不代表真实模型推理已通过。

真实聊天另需本机运行 `ollama serve` 并完成 `ollama pull qwen3:8b`。请在真实模型上验收：普通聊天、CPU／内存查询、查询后的追问，以及 Ollama 不可用时的错误提示。

当前尚未包含流式输出、重试、多模型回退、持久化记忆、任务队列、GPU 管理或桌宠 UI。
