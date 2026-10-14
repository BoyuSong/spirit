# AI Companion

当前仓库实现的是 **云端多模型 Agent Core**：主管、编码、网站、系统四个功能 Bot，每个角色使用独立 Discord Bot。支持用户直接调度和主管按需分派；Coding 可修改项目并运行 Python 测试，系统可下载、删除和恢复工作区文件。按用户当前设置，Coding 与系统操作自动执行并记录日志。内部任务数据使用独立 Discord 频道，默认全部使用云端模型。

安装、使用和测试见 [Agent 开发说明](README1.md)，角色配置和新增 Bot 见 [Discord 多模型设计](docs/cloud-multi-agent.md)。

以下保留早期 Windows 桌宠设计作为参考；其中本地模型及启动步骤不适用于当前云端 Agent，当前代码尚未包含该 UI 阶段。

## Live2D Companion MVP (Windows) 设计

桌宠阶段目标：透明无边框窗口、拖动/点击、系统托盘、状态面板、Ollama Qwen3 聊天、CPU/内存/磁盘/NVIDIA GPU 采样、内存阈值通知、SQLite 事件日志。

**当前不包含 Live2D SDK 和模型**。默认显示可交互占位角色；Live2D 渲染接入说明见 `live2d/README.md`。

## 安装（PowerShell）

```powershell
cd live2d_companion_mvp
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
ollama pull qwen3:8b
.\.venv\Scripts\python.exe main.py
```

安装 Ollama: https://ollama.com/download/windows
如果 `py -3.12` 不存在，先安装 Python 3.12，或者用已安装的兼容 Python 创建虚拟环境。

## 使用

- 单击占位桌宠：切换状态/聊天面板；按住拖动移动。
- 右下角系统托盘：显示/隐藏、打开面板、暂停/恢复监控、完全退出。
- 关闭面板仅隐藏面板，后台监控继续运行。
- `C:` 磁盘、CPU、内存、NVIDIA GPU 利用率每 3 秒采样一次。
- 内存使用率连续三次 >=90% 时触发一次警告，降到 85% 以下才重新允许警告。
- 日志位于 `%LOCALAPPDATA%\Live2DCompanion\events.sqlite3`。
- 聊天依赖本机 Ollama；如果 Ollama 不可用，面板会显示错误，不影响桌宠运行。

## 已知限制

- Windows 托盘气泡通知依赖系统通知设置，可能被勿扰模式屏蔽。
- GPU 查询依赖 NVIDIA `nvidia-smi`，缺失时显示 N/A。
- 监控暂停不暂停聊天；当前不包含自动系统修改、消息平台监听、专业模型路由或开机自启。
- Live2D 渲染需要官方 SDK / 模型；`file://` 模式不保证适配官方 Demo 的资源加载方式。
- WebEngine 模式当前仅支持顶部 38px 的拖动/点击区；透明区域的鼠标穿透与模型精准点击尚未实现。
- 聊天请求期间会阻止完全退出，防止强制销毁 Qt 工作线程。后续应加入请求取消。
- 本骨架尚未在用户的 Windows / RTX 5070 设备上实机验证。

## 后续迭代

1. 官方 Demo 集成、模型状态/表情桥接、逐像素命中测试。
2. Agent Core 独立进程与 IPC。
3. 任务队列、权限引擎、消息连接器、多模型路由。
4. Windows 登录自启动、通知中心、长期记忆、安装包。
