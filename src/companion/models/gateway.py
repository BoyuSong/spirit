from companion.models.base import ModelProvider, ModelRequest, ModelResponse
from companion.models.registry import ModelRegistry
from companion.models.providers.ollama import OllamaProvider
from companion.models.providers.openai import OpenAIProvider
from companion.models.providers.deepseek import DeepSeekProvider
from typing import Any


class ModelGateway:
    def __init__(self, registry: ModelRegistry):
        self.registry = registry
        self.providers: dict[tuple[str, ...], ModelProvider] = {}

    async def generate(
        self,
        role: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> ModelResponse:
        config = self.registry.get(role)
        if config.provider == "ollama":
            key = (config.provider, config.model, config.host)
            if key not in self.providers:
                self.providers[key] = OllamaProvider(model=config.model, host=config.host)
        elif config.provider == "openai":
            key = (config.provider, config.model, config.base_url, config.api_key_env)
            if key not in self.providers:
                self.providers[key] = OpenAIProvider(
                    model=config.model, base_url=config.base_url, api_key_env=config.api_key_env,
                )
        elif config.provider == "deepseek":
            key = (config.provider, config.model, config.base_url, config.api_key_env)
            if key not in self.providers:
                self.providers[key] = DeepSeekProvider(config.model, config.base_url, config.api_key_env)
        else:
            raise ValueError(f"Unsupported provider: {config.provider}")
        return await self.providers[key].generate(ModelRequest(
            messages=messages, tools=tools or [], response_format=response_format,
        ))
