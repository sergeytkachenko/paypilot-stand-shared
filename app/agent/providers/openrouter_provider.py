""
from app import config, runctx
from app.agent import models
from app.agent.providers.openai_provider import OpenAIProvider

API_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "anthropic/claude-haiku-5.5"


class OpenRouterProvider(OpenAIProvider):
    name = "openrouter"
    api_url = API_URL
    key_setting = "OPENROUTER_API_KEY"
    default_model = DEFAULT_MODEL

    def _system_content(self, system: str):
        block = {"type": "text", "text": system}
        if models.uses_cache_control(self.model):
            block["cache_control"] = {"type": "ephemeral"}
        return [block]

    def _extra_payload(self) -> dict:
        level = runctx.current().reasoning or config.LLM_REASONING
        return models.reasoning_payload(level)
