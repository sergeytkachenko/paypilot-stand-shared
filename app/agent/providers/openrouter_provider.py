""
from app.agent.providers.openai_provider import OpenAIProvider

API_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "anthropic/claude-haiku-5-5"


class OpenRouterProvider(OpenAIProvider):
    name = "openrouter"
    api_url = API_URL
    key_setting = "OPENROUTER_API_KEY"
    default_model = DEFAULT_MODEL

    def _headers(self) -> dict:
        return {**super()._headers(), "X-Title": "paypilot-stand"}
