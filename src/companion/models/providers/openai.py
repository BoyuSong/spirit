from copy import deepcopy
import json
import os
from typing import Any

import httpx

from companion.models.base import ModelProvider, ModelRequest, ModelResponse, ToolCall


def responses_input(messages: list[dict[str, Any]], *, model: str | None = None) -> list[dict[str, Any]]:
    """Translate canonical history, including older Ollama tool turns."""
    items = []
    pending: list[tuple[str, str]] = []
    for index, message in enumerate(messages):
        role = message["role"]
        if role == "tool":
            call_id = message.get("tool_call_id")
            if call_id is not None:
                match = next((pair for pair in pending if pair[1] == call_id), None)
            else:
                match = next((pair for pair in pending if pair[0] == message.get("tool_name")), None)
            if match is None:
                raise ValueError("Tool history is missing its matching function call")
            pending.remove(match)
            items.append({"type": "function_call_output", "call_id": match[1], "output": message["content"]})
            continue
        if pending:
            raise ValueError("Tool history has unanswered function calls")
        calls = message.get("tool_calls", [])
        pending = [
            (call["function"]["name"], call.get("id") or f"call_history_{index}_{number}")
            for number, call in enumerate(calls)
        ]
        if (
            role == "assistant" and message.get("provider_items")
            and (model is None or message.get("provider_model", model) == model)
        ):
            # Preserve encrypted reasoning and function-call items from Responses API.
            items.extend(deepcopy(message["provider_items"]))
            continue
        if message.get("content") or not calls:
            items.append({"role": role, "content": message.get("content", "")})
        for call, (_, call_id) in zip(calls, pending):
            items.append({
                "type": "function_call", "call_id": call_id,
                "name": call["function"]["name"],
                "arguments": json.dumps(call["function"]["arguments"], ensure_ascii=False, allow_nan=False),
            })
    if pending:
        raise ValueError("Tool history has unanswered function calls")
    return items


class OpenAIProvider(ModelProvider):
    def __init__(
        self, model: str, base_url: str = "https://api.openai.com/v1",
        api_key_env: str = "CODING_API_KEY", *, transport: httpx.AsyncBaseTransport | None = None,
    ):
        url = httpx.URL(base_url)
        if url.scheme not in ("http", "https") or not url.host or url.userinfo or url.query or url.fragment:
            raise ValueError("OpenAI base_url must be an HTTP(S) URL without credentials, query or fragment")
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.transport = transport

    async def generate(self, request: ModelRequest) -> ModelResponse:
        api_key = os.environ.get(self.api_key_env)
        if not api_key or not api_key.strip():
            raise ValueError(f"Set {self.api_key_env} in environment settings to use the coding model")
        payload = {
            "model": self.model, "input": responses_input(request.messages, model=self.model),
            "store": False, "include": ["reasoning.encrypted_content"],
        }
        if request.tools:
            payload["tools"] = [
                {"type": "function", **tool["function"], "strict": False} if tool["type"] == "function" else dict(tool)
                for tool in request.tools
            ]
            if any(tool["type"] == "web_search" for tool in request.tools):
                payload["max_tool_calls"] = 8
        if request.response_format:
            payload["text"] = {"format": request.response_format}
        # Use the model's sampling defaults; reasoning models may reject temperature.
        async with httpx.AsyncClient(timeout=120, transport=self.transport) as client:
            response = await client.post(
                f"{self.base_url}/responses", json=payload,
                headers={"Authorization": f"Bearer {api_key}"},
            )
        if response.is_error:
            raise RuntimeError(f"OpenAI request failed (HTTP {response.status_code}); check API access and model availability")
        try:
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("OpenAI response must be an object")
            if data.get("status") not in (None, "completed") or data.get("error"):
                raise ValueError("OpenAI response did not complete")
            output = data["output"]
            if not isinstance(output, list):
                raise ValueError("OpenAI output must be a list")
            content = []
            calls = []
            sources = []
            for item in output:
                if item["type"] == "message":
                    for part in item["content"]:
                        if part["type"] == "output_text":
                            content.append(part["text"])
                            for annotation in part.get("annotations", []):
                                if annotation.get("type") == "url_citation" and annotation.get("url") not in sources:
                                    url = annotation.get("url", "")
                                    if isinstance(url, str) and url.startswith(("https://", "http://")):
                                        sources.append(url)
                        elif part["type"] == "refusal":
                            content.append(part["refusal"])
                elif item["type"] == "function_call":
                    arguments = json.loads(item["arguments"])
                    if (
                        not isinstance(arguments, dict)
                        or not isinstance(item["call_id"], str) or not item["call_id"]
                        or not isinstance(item["name"], str) or not item["name"]
                    ):
                        raise ValueError("Invalid OpenAI function call")
                    calls.append(ToolCall(name=item["name"], arguments=arguments, id=item["call_id"]))
            if len({call.id for call in calls}) != len(calls):
                raise ValueError("Duplicate OpenAI function call IDs")
            if sources and not request.response_format:
                content.append("来源：\n" + "\n".join(sources))
            return ModelResponse(
                content="\n".join(content), model=data.get("model", self.model),
                tool_calls=calls, provider_items=output, provider_model=self.model,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("OpenAI returned an invalid or incomplete response") from exc
