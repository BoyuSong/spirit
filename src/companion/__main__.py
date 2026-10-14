import argparse
import asyncio
from pathlib import Path
from uuid import uuid4

from companion.collaboration.config import CollaborationConfig
from companion.collaboration.factory import build_agents
from companion.core.agent import Agent
from companion.models.gateway import ModelGateway
from companion.models.registry import ModelRegistry
from companion.tools.approval import ExecutionContext, execution_context
from companion.tools.registry import ToolRegistry


async def main(initial_role="coordinator", goal=None):
    root = Path(__file__).resolve().parents[2]
    registry = ModelRegistry(root / "config" / "models.yaml")
    config = CollaborationConfig.load(root / "config" / "agents.yaml")
    gateway = ModelGateway(registry)
    agents = build_agents(config, gateway)
    histories = {role: [{"role": "system", "content": spec.prompt}] for role, spec in config.agents.items()}
    role = initial_role
    if role not in agents:
        registry.get(role)
        agents[role] = Agent(gateway, ToolRegistry())
        histories[role] = [{"role": "system", "content": "Answer honestly; distinguish proposed actions from executed actions."}]
    print("AI Companion ready. /chat /coding /web /system; /role NAME; /exit. Manager dispatch: python -m companion.discord_workflow 'goal'")
    while True:
        try:
            user_input = goal if goal is not None else (await asyncio.to_thread(input, "You> ")).strip()
        except EOFError:
            break
        if user_input == "/exit":
            break
        requested = None
        if user_input in ("/chat", "/coordinator", "/coding", "/web", "/system"):
            requested = "coordinator" if user_input in ("/chat", "/coordinator") else user_input[1:]
        elif user_input.startswith("/role "):
            requested = user_input[6:].strip()
            requested = next((name for name, spec in config.agents.items() if spec.model_role == requested), requested)
        if requested is not None:
            try:
                if requested not in agents:
                    registry.get(requested)
                    agents[requested] = Agent(gateway, ToolRegistry())
                    histories[requested] = [{"role": "system", "content": "Answer honestly; distinguish proposed actions from executed actions."}]
                role = requested
                print(f"Agent: {role}")
            except ValueError as exc:
                print(f"Configuration error: {exc}")
            if goal is not None:
                break
            continue
        if not user_input.strip():
            if goal is not None:
                break
            continue
        model_role = config.agents[role].model_role if role in config.agents else role
        context_token = execution_context.set(ExecutionContext("terminal", role, str(uuid4())))
        try:
            result = await agents[role].run(model_role, histories[role] + [{"role": "user", "content": user_input}])
        except Exception as exc:
            print(f"Agent error: {exc}")
        else:
            print(f"[{result.response.model}] {result.response.content}\n")
            histories[role] = result.messages
        finally:
            execution_context.reset(context_token)
        if goal is not None:
            break


def entry():
    parser = argparse.ArgumentParser(description="Direct CLI access to a functional cloud agent")
    parser.add_argument("goal", nargs="?")
    parser.add_argument("--agent", default="coordinator")
    args = parser.parse_args()
    asyncio.run(main(args.agent, args.goal))


if __name__ == "__main__":
    entry()
