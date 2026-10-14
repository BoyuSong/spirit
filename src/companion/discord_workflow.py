"""Explicitly run a bounded model workflow on the configured Discord channel."""
import argparse
import asyncio
import os
from pathlib import Path

from companion.collaboration.config import CollaborationConfig
from companion.collaboration.discord import DiscordBus
from companion.collaboration.workflow import WorkflowRunner
from companion.collaboration.factory import build_agents
from companion.environment import load_environment
from companion.models.gateway import ModelGateway
from companion.models.registry import ModelRegistry


def check_models(config, registry, active_roles=None):
    roles = active_roles or config.agents
    missing = set()
    for role in roles:
        model = registry.get(config.agents[role].model_role)
        if model.provider not in ("openai", "deepseek"):
            raise ValueError("Discord workers require a supported cloud provider (openai or deepseek)")
        if config.agents[role].builtin_tools and model.provider != "openai":
            raise ValueError(f"{role} requires a provider supporting its native tools; remove or replace builtin_tools when changing provider")
        if not os.environ.get(model.api_key_env):
            missing.add(model.api_key_env)
    if missing:
        raise ValueError(f"Configure model credentials in environment settings: {', '.join(sorted(missing))}")


async def run(goal: str, target: str | None = None):
    root = Path(__file__).resolve().parents[2]
    load_environment(root)
    config = CollaborationConfig.load(root / "config" / "agents.yaml")
    registry = ModelRegistry(root / "config" / "models.yaml")
    if target is not None and target not in config.agents:
        raise ValueError(f"Unknown agent: {target}")
    active_roles = [target] if target else None
    check_models(config, registry, active_roles)
    bus = DiscordBus(config, observer=target)
    try:
        await bus.initialize(active_roles=active_roles)
        agents = build_agents(config, ModelGateway(registry))
        result = await WorkflowRunner(config, bus, agents).run(goal, target=target)
        print(f"Job {result.job_id}\n{result.content}")
    finally:
        await bus.close()


def main():
    parser = argparse.ArgumentParser(description="Run a cloud-model task using independent Discord bots")
    parser.add_argument("goal", help="Task goal; starting this command posts tasks/results to Discord")
    parser.add_argument("--agent", help="Dispatch directly to a functional bot, bypassing the manager")
    args = parser.parse_args()
    try:
        asyncio.run(run(args.goal, target=args.agent))
    except (RuntimeError, ValueError) as exc:
        parser.exit(1, f"Workflow error: {exc}\n")


if __name__ == "__main__":
    main()
