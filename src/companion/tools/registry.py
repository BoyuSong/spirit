import asyncio
import json
import math
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, ConfigDict, ValidationError

from companion.models.base import ToolCall


class ToolArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ToolError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    arguments: type[ToolArguments]
    handler: Callable[[Any], Awaitable[Any]]
    timeout: float = 5.0


class ToolRegistry:
    """Only explicitly registered tools can execute; arguments are validated first."""

    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        if not math.isfinite(tool.timeout) or tool.timeout <= 0:
            raise ValueError("Tool timeout must be finite and positive")
        self._tools[tool.name] = tool

    def schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.arguments.model_json_schema(),
                },
            }
            for tool in self._tools.values()
        ]

    async def execute(self, call: ToolCall) -> dict[str, Any]:
        tool = self._tools.get(call.name)
        if tool is None:
            return self._error("unknown_tool", f"Unknown tool: {call.name}")
        try:
            arguments = tool.arguments.model_validate(call.arguments, strict=True)
        except ValidationError:
            return self._error("invalid_arguments", "Arguments do not match the tool schema")
        try:
            result = await asyncio.wait_for(tool.handler(arguments), timeout=tool.timeout)
            json.dumps(result, allow_nan=False)
            return {"ok": True, "result": result}
        except TimeoutError:
            return self._error("tool_timeout", f"Tool timed out: {call.name}")
        except ToolError as exc:
            return self._error(exc.code, str(exc))
        except Exception as exc:
            return self._error("tool_failed", f"{call.name}: {type(exc).__name__}")

    @staticmethod
    def _error(code: str, message: str) -> dict[str, Any]:
        return {"ok": False, "error": {"code": code, "message": message}}
