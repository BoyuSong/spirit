"""Explicitly run a bounded model workflow on the configured Discord channel."""
import argparse
import asyncio
import os
from pathlib import Path

from companion.collaboration.config import CollaborationConfig
from companion.collaboration.discord import DiscordBus
from companion.collaboration.workflow import WorkflowRunner
from companion.core.agent import Agent
from companion.models.gateway import ModelGateway
from companion.models.registry import ModelRegistry
from companion.tools.system import default_tools


async def run(goal: str):
    root = Path(__file__).resolve().parents[2]
    config = CollaborationConfig.load(root / "config" / "agents.yaml")
    registry = ModelRegistry(root / "config" / "models.yaml")
    missing = set()
    for spec in config.agents.values():
        model = registry.get(spec.model_role)
        if model.provider != "openai":
            raise ValueError("Discord workers currently require cloud OpenAI models")
        if not os.environ.get(model.api_key_env):
            missing.add(model.api_key_env)
    if missing:
        raise ValueError(f"Configure model credentials in environment settings: {', '.join(sorted(missing))}")
    bus = DiscordBus(config)
    try:
        await bus.initialize()
        agent = Agent(ModelGateway(registry), default_tools())
        result = await WorkflowRunner(config, bus, agent).run(goal)
        print(f"Job {result.job_id}\n{result.content}")
    finally:
        await bus.close()


def main():
    parser = argparse.ArgumentParser(description="Run a cloud-model task using independent Discord bots")
    parser.add_argument("goal", help="Task goal; starting this command posts tasks/results to Discord")
    args = parser.parse_args()
    try:
        asyncio.run(run(args.goal))
    except (RuntimeError, ValueError) as exc:
        parser.exit(1, f"Workflow error: {exc}\n")


if __name__ == "__main__":
    main()
