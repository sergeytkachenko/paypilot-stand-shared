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


def _arguments(raw: str | None) -> dict:
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {"_unparsed_arguments": raw}
    return parsed if isinstance(parsed, dict) else {"_unparsed_arguments": raw}


def _to_openai(system: str | list[dict], messages: list[dict]) -> list[dict]:
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
    timeout = 60

    def __init__(self):
        self.api_key = getattr(config, self.key_setting)
        if not self.api_key:
            raise RuntimeError(f"{self.key_setting} is not set")
        self.model = config.LLM_MODEL or self.default_model

    def _system_content(self, system: str):
        return system

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"}

    def complete(self, system, messages, tools):
        payload = {
            "model": self.model,
            **_temperature(),
            "messages": _to_openai(self._system_content(system), messages),
        }
        if tools:
            payload["tools"] = [{"type": "function", "function": {
                "name": t["name"], "description": t["description"],
                "parameters": t["input_schema"]}} for t in tools]
        resp = httpx.post(self.api_url, json=payload, timeout=self.timeout,
                          headers=self._headers())
        resp.raise_for_status()
        data = resp.json()
        msg = data["choices"][0]["message"]
        tool_calls = [{"id": tc["id"], "name": tc["function"]["name"],
                       "arguments": _arguments(tc["function"]["arguments"])}
                      for tc in msg.get("tool_calls") or []]
        usage = data.get("usage") or {}
        details = usage.get("prompt_tokens_details") or {}
        return ModelResponse(
            text=msg.get("content"), tool_calls=tool_calls,
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            model=data.get("model", self.model),
            cache_read_input_tokens=details.get("cached_tokens") or 0,
            cache_creation_input_tokens=details.get("cache_write_tokens") or 0)
