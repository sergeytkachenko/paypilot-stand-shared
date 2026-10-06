""
import json

import httpx

from app import config
from app.agent.providers.base import ModelResponse, Provider


def _temperature() -> dict:
    if config.LLM_TEMPERATURE is None:
        return {}
    return {"temperature": config.LLM_TEMPERATURE}

API_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL = "gpt-5-mini"


def _to_openai(system: str, messages: list[dict]) -> list[dict]:
    out = [{"role": "system", "content": system}]
    for m in messages:
        if m["role"] == "user":
            out.append({"role": "user", "content": m["content"]})
        elif m["role"] == "assistant":
            msg = {"role": "assistant", "content": m.get("content")}
            if m.get("tool_calls"):
                msg["tool_calls"] = [{
                    "id": tc["id"], "type": "function",
                    "function": {"name": tc["name"],
                                 "arguments": json.dumps(tc["arguments"])}}
                    for tc in m["tool_calls"]]
            out.append(msg)
        elif m["role"] == "tool":
            out.append({"role": "tool", "tool_call_id": m["tool_call_id"],
                        "content": m["content"]})
    return out


class OpenAIProvider(Provider):
    name = "openai"

    def __init__(self):
        if not config.OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY is not set")
        self.model = config.LLM_MODEL or DEFAULT_MODEL

    def complete(self, system, messages, tools):
        payload = {
            "model": self.model,
            **_temperature(),
            "messages": _to_openai(system, messages),
        }
        if tools:
            payload["tools"] = [{"type": "function", "function": {
                "name": t["name"], "description": t["description"],
                "parameters": t["input_schema"]}} for t in tools]
        resp = httpx.post(API_URL, json=payload, timeout=60, headers={
            "Authorization": f"Bearer {config.OPENAI_API_KEY}"})
        resp.raise_for_status()
        data = resp.json()
        msg = data["choices"][0]["message"]
        tool_calls = [{"id": tc["id"], "name": tc["function"]["name"],
                       "arguments": json.loads(tc["function"]["arguments"] or "{}")}
                      for tc in msg.get("tool_calls") or []]
        usage = data.get("usage", {})
        return ModelResponse(
            text=msg.get("content"), tool_calls=tool_calls,
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            model=data.get("model", self.model))
