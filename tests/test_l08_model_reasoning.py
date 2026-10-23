import json

import pytest

from app import budget, config, otel, runctx
from app.agent import models, pricing
from app.agent.providers import openai_provider, openrouter_provider

GEMINI = "google/gemini-2.5-flash"
HAIKU = "anthropic/claude-haiku-5.5"


class _Resp:
    def __init__(self, model):
        self.model = model

    def raise_for_status(self):
        pass

    def json(self):
        return {"model": self.model,
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2}}


@pytest.fixture
def sent(monkeypatch):
    body = {}

    def fake_post(url, json, timeout, headers):
        body.update(json)
        return _Resp(json["model"])

    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setattr(config, "LLM_MODEL", "")
    monkeypatch.setattr(config, "LLM_REASONING", "")
    monkeypatch.setattr(openai_provider.httpx, "post", fake_post)
    return body


def _complete():
    return openrouter_provider.OpenRouterProvider().complete(
        "sys", [{"role": "user", "content": "hi"}], [])


def test_header_parses_model_and_reasoning():
    s = runctx.parse_header(json.dumps({"model": GEMINI, "reasoning": "high"}))
    assert (s.model, s.reasoning) == (GEMINI, "high")
    assert s.as_dict() == {"model": GEMINI, "reasoning": "high"}


def test_unknown_model_is_rejected_with_the_whitelist():
    with pytest.raises(ValueError, match="unknown model") as e:
        runctx.parse_header(json.dumps({"model": "openai/gpt-5"}))
    assert GEMINI in str(e.value) and HAIKU in str(e.value)


def test_unknown_reasoning_level_is_rejected():
    with pytest.raises(ValueError, match="reasoning must be one of"):
        runctx.parse_header(json.dumps({"reasoning": "extreme"}))


def test_every_whitelisted_model_has_a_price():
    assert models.known() == [HAIKU, GEMINI]
    for model in models.known():
        assert pricing.price_key(model) is not None


def test_request_model_overrides_server_model(sent, monkeypatch):
    monkeypatch.setattr(config, "LLM_MODEL", HAIKU)
    with runctx.override(model=GEMINI):
        resp = _complete()
    assert sent["model"] == GEMINI
    assert resp.model == GEMINI
    assert _complete().model == HAIKU


def test_reasoning_levels_map_to_the_openrouter_param(sent):
    _complete()
    assert "reasoning" not in sent
    sent.clear()
    with runctx.override(reasoning="off"):
        _complete()
    assert sent["reasoning"] == {"enabled": False}
    sent.clear()
    with runctx.override(reasoning="high"):
        _complete()
    assert sent["reasoning"] == {"effort": "high"}


def test_server_reasoning_applies_until_the_request_overrides_it(sent, monkeypatch):
    monkeypatch.setattr(config, "LLM_REASONING", "low")
    _complete()
    assert sent["reasoning"] == {"effort": "low"}
    sent.clear()
    with runctx.override(reasoning="off"):
        _complete()
    assert sent["reasoning"] == {"enabled": False}


def test_cache_control_follows_the_whitelist(sent, monkeypatch):
    monkeypatch.setitem(models.WHITELIST, GEMINI, {"cache_control": False})
    with runctx.override(model=GEMINI):
        provider = openrouter_provider.OpenRouterProvider()
    assert provider._system_content("sys") == [{"type": "text", "text": "sys"}]
    assert "cache_control" in openrouter_provider.OpenRouterProvider(
    )._system_content("sys")[0]


def test_gemini_price_matches_openrouter_cost_on_both_cache_calls():
    first = pricing.cost_usd(GEMINI, 7697, 65, cache_read=7667, cache_write=7667)
    second = pricing.cost_usd(GEMINI, 7697, 57, cache_read=7667, cache_write=0)
    assert first == pytest.approx(0.0010404, abs=2e-6)
    assert second == pytest.approx(0.00038151, abs=2e-6)


def test_gemini_overlapping_cache_buckets_sum_to_input_tokens():
    uncached, read, write = pricing.cache_buckets(GEMINI, 7697, 7667, 7667)
    assert (uncached, read, write) == (30, 0, 7667)
    assert uncached + read + write == 7697


def test_exclusive_cache_models_keep_their_buckets():
    assert pricing.cache_buckets(HAIKU, 10440, 0, 10326) == (114, 0, 10326)
    assert pricing.cache_buckets(HAIKU, 10440, 10326, 0) == (114, 10326, 0)


def test_langfuse_buckets_for_gemini_sum_to_input_tokens():
    buckets = otel.usage_details({
        "gen_ai.request.model": GEMINI, "gen_ai.usage.input_tokens": 7697,
        "gen_ai.usage.cache_read_input_tokens": 7667,
        "gen_ai.usage.cache_creation_input_tokens": 7667,
        "gen_ai.usage.output_tokens": 65})
    assert buckets["input"] + buckets["input_cached_tokens"] + buckets[
        "input_cache_creation_tokens"] == 7697


def test_reasoning_level_becomes_a_trace_tag():
    tree = {"name": "agent.turn", "attributes": {
        "run.profile": "lesson-08", "run.reasoning": "high"}, "children": []}
    assert "reasoning:high" in otel.trace_attributes(tree)["langfuse.trace.tags"]


def test_startup_refuses_a_whitelisted_model_without_a_price(monkeypatch):
    monkeypatch.setattr(config, "STAND_KEYS_REQUIRED", True)
    monkeypatch.setitem(models.WHITELIST, "vendor/unpriced-model", {})
    with pytest.raises(RuntimeError, match="whitelist=vendor/unpriced-model"):
        budget.validate_startup()
