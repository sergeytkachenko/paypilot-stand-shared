from app.agent import pricing


def test_haiku_cost_matches_the_list_price():
    assert pricing.cost_usd("claude-haiku-4-5", 2452, 114) == 0.003022


def test_dated_and_qualified_model_ids_resolve_to_the_same_price():
    expected = pricing.cost_usd("claude-haiku-4-5", 1000, 100)
    assert pricing.cost_usd("claude-haiku-4-5-20251001", 1000, 100) == expected
    assert pricing.cost_usd("anthropic/claude-haiku-4-5", 1000, 100) == expected
    assert pricing.cost_usd("Claude-Haiku-4-5", 1000, 100) == expected


def test_unknown_model_has_no_price():
    assert pricing.cost_usd("gpt-5-mini", 1000, 100) is None
    assert pricing.cost_usd("", 1000, 100) is None
    assert pricing.cost_usd(None, 1000, 100) is None


def test_total_is_empty_when_any_part_is_unknown():
    assert pricing.total_cost_usd([0.001, 0.002]) == 0.003
    assert pricing.total_cost_usd([0.001, None]) is None
    assert pricing.total_cost_usd([]) is None


def test_haiku_55_short_and_long_prompt_tiers():
    assert pricing.cost_usd("claude-haiku-5-5", 5266, 110) == 0.000582
    assert pricing.cost_usd("claude-haiku-5-5", 100_000, 0) == 0.01
    assert pricing.cost_usd("claude-haiku-5-5", 100_001, 1000) == round((100_001 * 0.5 + 1000 * 2.5) / 1_000_000, 6)


def test_anthropic_default_model_is_haiku_5_5_and_priced(monkeypatch):
    from app import config
    from app.agent.providers import anthropic_provider
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(config, "LLM_MODEL", "")
    model = anthropic_provider.AnthropicProvider().model
    assert model == "claude-haiku-5-5"
    assert pricing.price_key(model) == "claude-haiku-5-5"
