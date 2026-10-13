from companion.models.base import ModelRequest, ModelResponse
from companion.models.registry import ModelRegistry
from companion.models.providers.ollama import OllamaProvider
from typing import Any


class ModelGateway:
    def __init__(self, registry: ModelRegistry):
        self.registry = registry
        self.providers: dict[tuple[str, str, str], OllamaProvider] = {}

    async def generate(
        self,
        role: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> ModelResponse:
        config = self.registry.get(role)
        if config.provider != "ollama":
            raise ValueError(f"Unsupported provider: {config.provider}")
        key = (config.provider, config.model, config.host)
        if key not in self.providers:
            self.providers[key] = OllamaProvider(model=config.model, host=config.host)
        return await self.providers[key].generate(ModelRequest(messages=messages, tools=tools or []))
