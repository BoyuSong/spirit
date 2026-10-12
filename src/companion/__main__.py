import asyncio
from pathlib import Path
from companion.models.gateway import ModelGateway
from companion.models.registry import ModelRegistry
from companion.models.router import ModelRouter


async def main():
    config_path = Path(__file__).resolve().parents[2] / "config" / "models.yaml"
    gateway = ModelGateway(ModelRegistry(config_path))
    router = ModelRouter()
    history = [{"role": "system", "content": "You are a helpful Chinese-language desktop assistant."}]
    print("AI Companion ready. Enter /exit to quit, /coding or /chat to switch tasks.")
    task_type = "chat"
    while True:
        user_input = (await asyncio.to_thread(input, "You> ")).strip()
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
            response = await gateway.generate(router.select_role(task_type), history + [message])
        except Exception as exc:
            print(f"Model error: {exc}")
            continue
        print(f"[{response.model}] {response.content}\n")
        history.extend([message, {"role": "assistant", "content": response.content}])


if __name__ == "__main__":
    asyncio.run(main())
