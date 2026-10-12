from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ModelRequest:
    messages: list[dict[str, str]]
    temperature: float = 0.3


@dataclass
class ModelResponse:
    content: str
    model: str


class ModelProvider(ABC):
    @abstractmethod
    async def generate(self, request: ModelRequest) -> ModelResponse:
        raise NotImplementedError
