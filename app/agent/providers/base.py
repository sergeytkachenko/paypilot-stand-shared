""
from dataclasses import dataclass, field


@dataclass
class ModelResponse:
    text: str | None = None
    tool_calls: list[dict] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""


class Provider:
    name = "base"

    def complete(self, system: str, messages: list[dict],
                 tools: list[dict]) -> ModelResponse:
        raise NotImplementedError


class Metered:
    def __init__(self, inner: Provider):
        object.__setattr__(self, "_inner", inner)

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def __setattr__(self, name, value):
        setattr(self._inner, name, value)

    def complete(self, system: str, messages: list[dict],
                 tools: list[dict]) -> ModelResponse:
        from app import budget
        resp = self._inner.complete(system, messages, tools)
        budget.charge(resp.model or getattr(self._inner, "model", ""),
                      resp.input_tokens, resp.output_tokens)
        return resp


def get_provider():
    from app import config
    kind = config.LLM_PROVIDER
    if kind == "mock":
        from app.agent.providers.mock import MockProvider
        return Metered(MockProvider())
    if kind == "anthropic":
        from app.agent.providers.anthropic_provider import AnthropicProvider
        return Metered(AnthropicProvider())
    if kind == "openai":
        from app.agent.providers.openai_provider import OpenAIProvider
        return Metered(OpenAIProvider())
    raise ValueError(f"Unknown LLM_PROVIDER={kind!r} (mock | anthropic | openai)")
