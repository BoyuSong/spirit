from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]
    id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        call: dict[str, Any] = {"function": {"name": self.name, "arguments": self.arguments}}
        if self.id:
            call["id"] = self.id
        return call


@dataclass
class ModelRequest:
    messages: list[dict[str, Any]]
    temperature: float = 0.3
    tools: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ModelResponse:
    content: str
    model: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    thinking: str | None = None
    provider_items: list[dict[str, Any]] = field(default_factory=list)

    def as_message(self) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            message["tool_calls"] = [call.as_dict() for call in self.tool_calls]
        if self.thinking:
            message["thinking"] = self.thinking
        if self.provider_items:
            message["provider_items"] = self.provider_items
        return message


class ModelProvider(ABC):
    @abstractmethod
    async def generate(self, request: ModelRequest) -> ModelResponse:
        raise NotImplementedError
