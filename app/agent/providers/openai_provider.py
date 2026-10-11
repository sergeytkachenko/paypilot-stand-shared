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
    api_url = API_URL
    key_setting = "OPENAI_API_KEY"
    default_model = DEFAULT_MODEL

    def __init__(self):
        self.api_key = getattr(config, self.key_setting)
        if not self.api_key:
            raise RuntimeError(f"{self.key_setting} is not set")
        self.model = config.LLM_MODEL or self.default_model

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"}

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
        resp = httpx.post(self.api_url, json=payload, timeout=60,
                          headers=self._headers())
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
