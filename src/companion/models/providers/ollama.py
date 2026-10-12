from ollama import AsyncClient
from companion.models.base import ModelProvider, ModelRequest, ModelResponse, ToolCall


class OllamaProvider(ModelProvider):
    def __init__(self, model: str, host: str = "http://127.0.0.1:11434"):
        self.model = model
        self.client = AsyncClient(host=host, timeout=120)

    async def generate(self, request: ModelRequest) -> ModelResponse:
        result = await self.client.chat(
            model=self.model,
            messages=request.messages,
            options={"temperature": request.temperature},
            tools=request.tools or None,
            stream=False,
        )
        return ModelResponse(
            content=result.message.content or "",
            model=self.model,
            tool_calls=[
                ToolCall(name=call.function.name, arguments=dict(call.function.arguments))
                for call in (result.message.tool_calls or [])
            ],
            thinking=getattr(result.message, "thinking", None),
        )
