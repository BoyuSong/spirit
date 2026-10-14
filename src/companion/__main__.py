import asyncio
from pathlib import Path
from companion.models.gateway import ModelGateway
from companion.models.registry import ModelRegistry
from companion.models.router import ModelRouter
from companion.core.agent import Agent
from companion.tools.system import default_tools


async def main():
    config_path = Path(__file__).resolve().parents[2] / "config" / "models.yaml"
    registry = ModelRegistry(config_path)
    gateway = ModelGateway(registry)
    agent = Agent(gateway, default_tools())
    router = ModelRouter()
    history = [{"role": "system", "content": (
        "You are a helpful Chinese-language desktop assistant. "
        "Use get_system_status for current CPU or memory measurements. "
        "Base your answer on the returned tool data; report tool failures honestly."
    )}]
    print("AI Companion ready. /chat /coding /review /research switch roles; /role NAME selects a configured model; /exit quits.")
    model_role = router.select_role("chat")
    while True:
        try:
            user_input = (await asyncio.to_thread(input, "You> ")).strip()
        except EOFError:
            break
        if user_input == "/exit":
            break
        if user_input in ("/coding", "/chat", "/coordinator", "/review", "/research"):
            task_type = user_input[1:]
            model_role = router.select_role(task_type)
            print(f"Task type: {task_type}")
            continue
        if user_input.startswith("/role "):
            requested_role = user_input[6:].strip()
            try:
                registry.get(requested_role)
            except ValueError as exc:
                print(f"Configuration error: {exc}")
            else:
                model_role = requested_role
                print(f"Model role: {model_role}")
            continue
        if not user_input:
            continue
        message = {"role": "user", "content": user_input}
        try:
            result = await agent.run(model_role, history + [message])
        except Exception as exc:
            print(f"Agent error: {exc}")
            continue
        print(f"[{result.response.model}] {result.response.content}\n")
        history = result.messages


if __name__ == "__main__":
    asyncio.run(main())
