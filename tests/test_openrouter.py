import pytest

from app import budget, config
from app.agent import pricing
from app.agent.providers import base, openai_provider, openrouter_provider


class _Resp:
    def raise_for_status(self):
        pass

    def json(self):
        return {"model": "anthropic/claude-haiku-5-5",
                "choices": [{"message": {"content": None, "tool_calls": [{
                    "id": "c1", "type": "function",
                    "function": {"name": "get_account", "arguments": "{}"}}]}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 3}}


def test_openrouter_is_selected_and_metered(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "openrouter")
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-or-test")
    provider = base.get_provider()
    assert isinstance(provider, base.Metered)
    assert provider.name == "openrouter"


def test_openrouter_requires_its_own_key(monkeypatch):
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "")
    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-openai")
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY is not set"):
        openrouter_provider.OpenRouterProvider()


def test_openrouter_posts_openai_format_to_openrouter(monkeypatch):
    sent = {}

    def fake_post(url, json, timeout, headers):
        sent.update(url=url, body=json, headers=headers)
        return _Resp()

    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setattr(config, "LLM_MODEL", "")
    monkeypatch.setattr(openai_provider.httpx, "post", fake_post)
    tools = [{"name": "get_account", "description": "d",
              "input_schema": {"type": "object", "properties": {}}}]
    resp = openrouter_provider.OpenRouterProvider().complete(
        "sys", [{"role": "user", "content": "balance?"}], tools)

    assert sent["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert sent["headers"]["Authorization"] == "Bearer sk-or-test"
    assert sent["body"]["model"] == "anthropic/claude-haiku-5-5"
    assert sent["body"]["messages"][0] == {"role": "system", "content": "sys"}
    assert sent["body"]["tools"][0]["function"]["name"] == "get_account"
    assert resp.tool_calls == [{"id": "c1", "name": "get_account", "arguments": {}}]
    assert (resp.input_tokens, resp.output_tokens) == (12, 3)
    assert pricing.cost_usd(resp.model, 12, 3) is not None


def test_openai_still_posts_to_openai(monkeypatch):
    sent = {}

    def fake_post(url, json, timeout, headers):
        sent.update(url=url, headers=headers)
        return _Resp()

    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-openai")
    monkeypatch.setattr(openai_provider.httpx, "post", fake_post)
    openai_provider.OpenAIProvider().complete("s", [{"role": "user", "content": "u"}], [])
    assert sent["url"] == "https://api.openai.com/v1/chat/completions"
    assert sent["headers"] == {"Authorization": "Bearer sk-openai"}


def test_openrouter_default_model_passes_the_priced_startup_check(monkeypatch):
    monkeypatch.setattr(config, "STAND_KEYS_REQUIRED", True)
    monkeypatch.setattr(config, "LLM_PROVIDER", "openrouter")
    monkeypatch.setattr(config, "LLM_MODEL", "")
    monkeypatch.setattr(config, "EXPLAIN_MODEL", "")
    monkeypatch.setattr(config, "ROUTER", "")
    assert budget.effective_models() == [("LLM_MODEL", "anthropic/claude-haiku-5-5")]
    budget.validate_startup()


def test_compose_passes_the_openrouter_key():
    compose = (config.ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "OPENROUTER_API_KEY=${OPENROUTER_API_KEY:-}" in compose
