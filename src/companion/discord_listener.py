"""Explicit entry point for Discord @Bot requests and human operation approvals."""
import asyncio
import argparse
from pathlib import Path

from companion.collaboration.config import CollaborationConfig
from companion.collaboration.discord import DiscordBus
from companion.collaboration.factory import build_agents, system_root
from companion.collaboration.hub import DiscordHub
from companion.discord_workflow import check_models
from companion.environment import load_environment
from companion.models.gateway import ModelGateway
from companion.models.registry import ModelRegistry
from companion.tools.approval import ApprovalManager


async def run():
    root = Path(__file__).resolve().parents[2]
    load_environment(root)
    config = CollaborationConfig.load(root / "config" / "agents.yaml")
    registry = ModelRegistry(root / "config" / "models.yaml")
    check_models(config, registry)
    bus = DiscordBus(config)
    try:
        await bus.initialize(require_operators=True)
        approvals = ApprovalManager(system_root(config) / ".agent-internal" / "audit.jsonl", terminal=False)
        hub = DiscordHub(config, bus, build_agents(config, ModelGateway(registry), approvals), approvals)
        print("Discord listener ready. Only configured operators can dispatch or approve tasks.")
        await hub.serve()
    finally:
        await bus.close()


def main():
    argparse.ArgumentParser(description="Listen for authorized Discord @Bot tasks and operation approvals").parse_args()
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(f"Listener error: {exc}") from exc


if __name__ == "__main__":
    main()
