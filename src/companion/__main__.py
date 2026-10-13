import asyncio
from pathlib import Path
from companion.models.gateway import ModelGateway
from companion.models.registry import ModelRegistry
from companion.models.router import ModelRouter
from companion.core.agent import Agent
from companion.tools.system import default_tools


async def main():
    config_path = Path(__file__).resolve().parents[2] / "config" / "models.yaml"
    gateway = ModelGateway(ModelRegistry(config_path))
    agent = Agent(gateway, default_tools())
    router = ModelRouter()
    history = [{"role": "system", "content": (
        "You are a helpful Chinese-language desktop assistant. "
        "Use get_system_status for current CPU or memory measurements. "
        "Base your answer on the returned tool data; report tool failures honestly."
    )}]
    print("AI Companion ready. Enter /exit to quit, /coding or /chat to switch tasks.")
    task_type = "chat"
    while True:
        try:
            user_input = (await asyncio.to_thread(input, "You> ")).strip()
        except EOFError:
            break
        if user_input == "/exit":
            break
        if user_input in ("/coding", "/chat"):
            task_type = user_input[1:]
            print(f"Task type: {task_type}")
            continue
        if not user_input:
            continue
        message = {"role": "user", "content": user_input}
        try:
            result = await agent.run(router.select_role(task_type), history + [message])
        except Exception as exc:
            print(f"Agent error: {exc}")
            continue
        print(f"[{result.response.model}] {result.response.content}\n")
        history = result.messages


if __name__ == "__main__":
    asyncio.run(main())
