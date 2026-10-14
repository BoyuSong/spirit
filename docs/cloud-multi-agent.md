# 云端多模型与 Discord Bot

每个角色绑定一个云端模型和一个独立 Discord Bot。首版由一个调度进程托管所有角色，模型之间的每次任务交接都经 Discord 消息发送、读取并校验；后续可以将 worker 拆为独立进程。

```mermaid
flowchart LR
    U[用户任务] --> C1[协调 Bot：拆解]
    C1 -->|Discord| R[研究 Bot]
    R -->|Discord| D[编码 Bot]
    D -->|Discord| V[审查 Bot]
    V -->|Discord| C2[协调 Bot：汇总]
```

## 角色配置

| 角色 | 默认模型 | 模型配置键 | Bot 凭据变量 |
|---|---|---|---|
| 协调 | `gpt-6-luna` | `primary` | `DISCORD_COORDINATOR_BOT_TOKEN` |
| 编码 | `gpt-6.1-sol` | `coding` | `DISCORD_CODING_BOT_TOKEN` |
| 审查 | `gpt-6-sol` | `review` | `DISCORD_REVIEW_BOT_TOKEN` |
| 研究 | `gpt-6-astra` | `research` | `DISCORD_RESEARCH_BOT_TOKEN` |

这些型号列于 OpenAI 官方 SDK，账户可用性与额度需实际验证。编码模型延续 GPT6.1 的选择；其他型号是可更换的初始配置。四个模型默认复用 `CODING_API_KEY`，也可在各自模型配置中指定不同的 `api_key_env`。

`config/models.yaml` 管理模型、服务端点和 API Key 变量名。`config/agents.yaml` 管理角色提示词、模型配置键、Bot Token 变量名、频道变量名、流程顺序和预算。Token 和 Key 都从进程环境读取，配置文件中没有凭据值。

## Discord 设置与启动

1. 在 Discord Developer Portal 创建四个独立应用及 Bot，并将它们添加到专用服务器频道。不能复用同一个 Bot 身份作为不同角色。
2. 给 Bot 配置该频道的 View Channel、Read Message History、Send Messages 和 Attach Files 权限。
3. 为协调 Bot 开启 **Message Content Intent**，使它能读取其他 Bot 的任务内容和附件。程序启动时检查这个设置；HTTP REST 读取同样受该权限限制。
4. 在环境设置中安全填写四个 Bot Token、`CODING_API_KEY`，以及非秘密的 `DISCORD_CHANNEL_ID`。保存并发布云环境配置。本地运行时通过本机环境或凭据管理提供这些变量。
5. 确认依赖安装完毕，运行：

```bash
python -m companion.discord_workflow "设计一个带测试的配置加载器"
```

在已设置 `PYTHONPATH` 的虚拟环境中运行。这个命令会主动向所选 Discord 频道发布任务和结果，并调用云端模型。`--help` 只显示帮助。当前没有后台服务开机自动运行，也不会自动把普通聊天转换为任务。

首版使用 Discord REST API 轮询，无需 Gateway WebSocket。网络目的地为 `api.openai.com`、`discord.com` 和用于下载任务附件的 `cdn.discordapp.com`。Bot 授权头只发送到 `discord.com`，不发送给附件 CDN。

## 消息和任务边界

- 版本化结构消息包含 `job_id`、`task_id`、发送／接收角色、阶段序号、过期时间、任务内容和前序结果。
- 只接受已登记 Bot 身份发出的消息，发送者必须与声明的角色一致。普通聊天、其他 Bot、伪造角色和过期消息不会执行。
- 一次运行只处理自己创建的 job；任务 ID 和阶段必须与当前步骤匹配。重复投递在该次运行中只执行一次，结果不能在对应任务执行前推进流程。
- 默认执行 5 个阶段：协调、研究、编码、审查、协调汇总。步骤由配置指定，最多 16 个；单任务默认 600 秒超时，前序结果上下文最多 64000 字符。
- 每个阶段仍使用 Agent Core 的 3 轮／8 次工具调用预算、120 秒模型等待和 5 秒工具等待。
- 每个任务／结果最多 32000 字符，协议包最多 512 KiB。超过 Discord 的 2000 UTF-16 字符单元限制时，以 JSON 附件传输；不会静默截断代码或结果。
- Discord 429 按服务器的 `retry_after` 等待，最多重试 3 次；未知的发送失败不自动重发。消息使用 nonce 去重，并禁止自动提及用户或角色。
- 模型失败通过该角色的 Bot 发布错误并终止 job；不会让模型互相无限回复。模型不能自行创建新的角色或扩张流程。
- Discord 传递任务和最终内容，不传递 API Key、Bot Token 或模型内部 reasoning。单模型工具循环中的加密 reasoning 只在对应模型的 API 调用链中保留。

## 新增模型和 Bot

例如新增 `analysis`：

```yaml
# config/models.yaml 的 models 下
analysis:
  provider: openai
  model: gpt-6-sol
  api_key_env: CODING_API_KEY
```

```yaml
# config/agents.yaml 的 agents 下
analysis:
  model_role: analysis
  token_env: DISCORD_ANALYSIS_BOT_TOKEN
  prompt: 你负责分析方案的性能和复杂度，区分推断与实测结果。
```

注册对应的新 Bot，提供 `DISCORD_ANALYSIS_BOT_TOKEN`，并将 `analysis` 插入 `workflow`。使用同一文字任务接口的新角色无需修改调度器。当前最多登记 32 个角色。

新增服务商需要实现 `ModelProvider` 适配；图像、音频等输入需要扩展模型／工具层。角色注册、Bot 身份和任务交接机制可以复用。

## 当前能力与验证边界

当前为有界串行流程，可生成研究、代码、审查意见和汇总。编码角色不能实际改文件或运行测试，研究角色没有实时联网检索工具。唯一内置工具读取运行机器的 CPU／内存状态。

当前 job 状态保存在内存中；重启会跳过旧频道消息，不恢复或重新执行中断 job。持久化队列、并行 DAG、自动修正迭代、独立 worker 部署和更多工具留作后续阶段。

测试使用模拟 Discord／OpenAI HTTP 服务，覆盖四个独立身份、实际 Model Gateway 和 Agent Core、系统工具、长附件、消息过滤、重复投递、预算和超时。真实 API 模型及 Discord 联调需要有效凭据、频道权限和账户访问；离线测试通过不等同于这些操作已验证。

参考：[Discord 消息 API](https://docs.discord.com/developers/resources/message)、[限流](https://docs.discord.com/developers/topics/rate-limits)、[Message Content Intent](https://docs.discord.com/developers/events/gateway#message-content-intent)、[OpenAI 模型 ID](https://github.com/openai/openai-python/blob/main/src/openai/types/shared/chat_model.py)。
