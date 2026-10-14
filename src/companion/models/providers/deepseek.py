"""DeepSeek's OpenAI-compatible Chat Completions interface."""
import json
import os

import httpx
from openai import APIConnectionError, APIStatusError, AsyncOpenAI

from companion.models.base import ModelAPIError, ModelProvider, ModelRequest, ModelResponse, ToolCall


def chat_messages(messages, *, model):
    result = []
    pending = []
    for index, message in enumerate(messages):
        role = message["role"]
        if role == "tool":
            call_id = message.get("tool_call_id")
            match = next((pair for pair in pending if (
                pair[1] == call_id if call_id is not None else pair[0] == message.get("tool_name")
            )), None)
            if match is None:
                raise ValueError("Tool history is missing its matching function call")
            pending.remove(match)
            result.append({"role": "tool", "tool_call_id": match[1], "content": message["content"]})
            continue
        if pending:
            raise ValueError("Tool history has unanswered function calls")
        item = {"role": role, "content": message.get("content", "")}
        calls = message.get("tool_calls", [])
        if calls:
            pending = [(call["function"]["name"], call.get("id") or f"call_history_{index}_{number}")
                       for number, call in enumerate(calls)]
            item["tool_calls"] = [
                {"id": call_id, "type": "function", "function": {
                    "name": call["function"]["name"],
                    "arguments": json.dumps(call["function"]["arguments"], ensure_ascii=False, allow_nan=False),
                }} for call, (_, call_id) in zip(calls, pending)
            ]
        # Reasoning-mode tool turns may require this field. It is provider/model
        # scoped and never sent to Discord or to the OpenAI Responses endpoint.
        if role == "assistant" and message.get("provider_model") == f"deepseek:{model}":
            for native in message.get("provider_items", []):
                if isinstance(native.get("reasoning_content"), str):
                    item["reasoning_content"] = native["reasoning_content"]
        result.append(item)
    if pending:
        raise ValueError("Tool history has unanswered function calls")
    return result


class DeepSeekProvider(ModelProvider):
    def __init__(self, model, base_url="https://api.deepseek.com/v1", api_key_env="DEEPSEEK_API_KEY", *, transport=None,
                 thinking=None, reasoning_effort=None):
        url = httpx.URL(base_url)
        if url.scheme != "https" or not url.host or url.userinfo or url.query or url.fragment:
            raise ValueError("DeepSeek base_url must be HTTPS without credentials, query or fragment")
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.transport = transport
        if thinking not in (None, "enabled", "disabled") or reasoning_effort not in (None, "low", "medium", "high"):
            raise ValueError("Invalid DeepSeek thinking configuration")
        self.thinking = thinking
        self.reasoning_effort = reasoning_effort

    async def generate(self, request: ModelRequest) -> ModelResponse:
        key = os.environ.get(self.api_key_env)
        if not key or not key.strip():
            raise ValueError(f"Set {self.api_key_env} in environment settings to use DeepSeek")
        payload = {"model": self.model, "messages": chat_messages(request.messages, model=self.model), "stream": False}
        if self.thinking is not None:
            payload["extra_body"] = {"thinking": {"type": self.thinking}}
        if self.reasoning_effort is not None:
            payload["reasoning_effort"] = self.reasoning_effort
        if request.tools:
            if any(tool.get("type") != "function" for tool in request.tools):
                raise ValueError("DeepSeek adapter supports function tools; native web_search requires a compatible provider")
            payload["tools"] = request.tools
        if request.response_format:
            kind = request.response_format.get("type")
            if kind not in ("json_schema", "json_object"):
                raise ValueError("Unsupported DeepSeek response format")
            # Chat Completions JSON mode guarantees an object, not our schema.
            # The dispatcher validates the decision locally before delegating.
            payload["response_format"] = {"type": "json_object"}
            instruction = "Return only a JSON object."
            if kind == "json_schema":
                instruction += " Follow this JSON schema: " + json.dumps(request.response_format["schema"], ensure_ascii=False)
            payload["messages"].insert(0, {"role": "system", "content": instruction})
        try:
            async with AsyncOpenAI(api_key=key, base_url=self.base_url, timeout=120, max_retries=0,
                    http_client=httpx.AsyncClient(timeout=120, transport=self.transport, follow_redirects=False)) as client:
                response = await client.chat.completions.create(**payload)
            data = response.model_dump(mode="json")
            if data.get("error") or len(data["choices"]) != 1:
                raise ValueError("Invalid DeepSeek choices")
            choice = data["choices"][0]
            if choice["finish_reason"] not in ("stop", "tool_calls"):
                raise ValueError("DeepSeek response did not complete")
            message = choice["message"]
            if message["role"] != "assistant":
                raise ValueError("Unexpected response role")
            content = message.get("content") or ""
            if not isinstance(content, str):
                raise ValueError("Invalid response content")
            calls = []
            for call in message.get("tool_calls") or []:
                arguments = json.loads(call["function"]["arguments"])
                if (call["type"] != "function" or not isinstance(arguments, dict)
                    or not isinstance(call["id"], str) or not call["id"]
                    or not isinstance(call["function"]["name"], str) or not call["function"]["name"]):
                    raise ValueError("Invalid function call")
                calls.append(ToolCall(call["function"]["name"], arguments, call["id"]))
            if len({call.id for call in calls}) != len(calls) or bool(calls) != (choice["finish_reason"] == "tool_calls"):
                raise ValueError("Invalid tool-call completion")
            reasoning = message.get("reasoning_content")
            native = [{"reasoning_content": reasoning}] if isinstance(reasoning, str) else []
            return ModelResponse(content, data.get("model", self.model), calls,
                                 provider_items=native, provider_model=f"deepseek:{self.model}")
        except APIStatusError as exc:
            raise ModelAPIError(f"DeepSeek request failed (HTTP {exc.status_code}); check API access and model availability") from None
        except APIConnectionError:
            raise ModelAPIError("DeepSeek connection failed; check network access and service availability") from None
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise ValueError("DeepSeek returned an invalid or incomplete response") from exc
