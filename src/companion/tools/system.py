import asyncio
import platform

import psutil

from companion.tools.registry import Tool, ToolArguments, ToolRegistry


def _system_status() -> dict:
    cpu_percent = psutil.cpu_percent(interval=0.1)
    memory = psutil.virtual_memory()
    return {
        "os": platform.system(),
        "cpu_count": psutil.cpu_count(),
        "cpu_percent": cpu_percent,
        "cpu_sample_seconds": 0.1,
        "memory": {
            "total_bytes": memory.total,
            "available_bytes": memory.available,
            "used_bytes": memory.used,
            "percent": memory.percent,
        },
    }


async def get_system_status(_: ToolArguments) -> dict:
    return await asyncio.to_thread(_system_status)


def default_tools() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(Tool(
        name="get_system_status",
        description="Read current CPU usage and memory usage on this machine. Takes no arguments.",
        arguments=ToolArguments,
        handler=get_system_status,
    ))
    return registry
