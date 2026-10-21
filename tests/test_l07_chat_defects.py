import json
import re

from fastapi.testclient import TestClient

from app import config
from app.agent import loop, pricing, prompt, tools
from app.agent.providers import anthropic_provider, openai_provider, openrouter_provider
from app.agent.providers.base import ModelResponse
from app.main import app

client = TestClient(app)


class _OpenAIResp:
    def __init__(self, usage):
        self.usage = usage

    def raise_for_status(self):
        pass

    def json(self):
        return {"model": "anthropic/claude-haiku-5.5",
                "choices": [{"message": {"content": "ok"}}],
                "usage": self.usage}


def _openrouter_call(monkeypatch, usage):
    sent = {}

    def fake_post(url, json, timeout, headers):
        sent.update(body=json)
        return _OpenAIResp(usage)

    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setattr(config, "LLM_MODEL", "")
    monkeypatch.setattr(openai_provider.httpx, "post", fake_post)
    resp = openrouter_provider.OpenRouterProvider().complete(
        "sys", [{"role": "user", "content": "hi"}], [])
    return sent["body"], resp


def test_openrouter_marks_the_system_prompt_cacheable(monkeypatch):
    body, _ = _openrouter_call(monkeypatch, {"prompt_tokens": 1, "completion_tokens": 1})
    assert body["messages"][0] == {"role": "system", "content": [
        {"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}]}


def test_openrouter_reports_cached_and_written_tokens(monkeypatch):
    _, resp = _openrouter_call(monkeypatch, {
        "prompt_tokens": 2601, "completion_tokens": 20,
        "prompt_tokens_details": {"cached_tokens": 2514, "cache_write_tokens": 0}})
    assert (resp.input_tokens, resp.cache_read_input_tokens,
            resp.cache_creation_input_tokens) == (2601, 2514, 0)


def test_openai_keeps_a_plain_system_string_and_reads_cached_tokens(monkeypatch):
    sent = {}

    def fake_post(url, json, timeout, headers):
        sent.update(body=json)
        return _OpenAIResp({"prompt_tokens": 3000, "completion_tokens": 5,
                            "prompt_tokens_details": {"cached_tokens": 1024}})

    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-openai")
    monkeypatch.setattr(openai_provider.httpx, "post", fake_post)
    resp = openai_provider.OpenAIProvider().complete("s", [{"role": "user", "content": "u"}], [])
    assert sent["body"]["messages"][0] == {"role": "system", "content": "s"}
    assert resp.cache_read_input_tokens == 1024


def test_malformed_tool_arguments_do_not_crash_the_turn(monkeypatch):
    class Resp(_OpenAIResp):
        def json(self):
            return {"model": "m", "usage": {},
                    "choices": [{"message": {"content": None, "tool_calls": [{
                        "id": "c1", "type": "function",
                        "function": {"name": "get_account", "arguments": "{not json"}}]}}]}

    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-openai")
    monkeypatch.setattr(openai_provider.httpx, "post", lambda *a, **k: Resp({}))
    resp = openai_provider.OpenAIProvider().complete("s", [{"role": "user", "content": "u"}], [])
    assert resp.tool_calls[0]["arguments"] == {"_unparsed_arguments": "{not json"}


def test_anthropic_caches_the_system_prompt_and_counts_cached_tokens_as_input(monkeypatch):
    sent = {}

    class Resp:
        status_code = 200

        def json(self):
            return {"model": "claude-haiku-5-5", "content": [{"type": "text", "text": "ok"}],
                    "usage": {"input_tokens": 87, "output_tokens": 20,
                              "cache_read_input_tokens": 2514,
                              "cache_creation_input_tokens": 0}}

    def fake_post(payload, headers, attempts, timeout):
        sent.update(payload=payload)
        return Resp()

    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(config, "LLM_MODEL", "")
    monkeypatch.setattr(anthropic_provider, "_post_with_retry", fake_post)
    resp = anthropic_provider.AnthropicProvider().complete(
        "sys", [{"role": "user", "content": "hi"}], [])
    assert sent["payload"]["system"] == [
        {"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}]
    assert (resp.input_tokens, resp.cache_read_input_tokens) == (2601, 2514)


def test_cached_tokens_are_priced_as_measured_on_openrouter():
    model = "anthropic/claude-haiku-5.5"
    assert pricing.cost_usd(model, 2598, 20) == 0.00027
    assert pricing.cost_usd(model, 2601, 20, cache_write=2514) == 0.000333
    assert pricing.cost_usd(model, 2601, 20, cache_read=2514) == 0.000044


class _Recorder:
    name = "anthropic"
    model = "claude-haiku-5-5"

    def __init__(self, systems):
        self.systems = systems
        self.calls = 0

    def complete(self, system, messages, tool_specs):
        self.systems.append(system)
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(tool_calls=[{"id": "t1", "name": "get_account", "arguments": {}}],
                                 input_tokens=3000, output_tokens=5, model=self.model,
                                 cache_creation_input_tokens=2900)
        return ModelResponse(text="done", input_tokens=3100, output_tokens=5, model=self.model,
                             cache_read_input_tokens=2900)


def _turn(monkeypatch, settings=None):
    systems = []
    recorder = _Recorder(systems)
    monkeypatch.setattr(loop, "get_provider", lambda: recorder)
    headers = {"X-Stand-Settings": json.dumps(settings)} if settings else {}
    out = client.post("/chat", json={"message": "What is my balance?",
                                     "customer_id": "CUS-0001"}, headers=headers).json()
    return systems, out


def test_trace_carries_cache_tokens_per_call_and_per_turn(monkeypatch):
    _, out = _turn(monkeypatch)
    tree = client.get(f"/api/_test/traces/{out['request_id']}").json()
    calls = [c for c in tree["children"] if c["name"] == "llm.call"]
    assert [c["attributes"]["gen_ai.usage.cache_read_input_tokens"] for c in calls] == [0, 2900]
    assert [c["attributes"]["gen_ai.usage.cache_creation_input_tokens"] for c in calls] == [2900, 0]
    assert tree["attributes"]["gen_ai.usage.total_cache_read_input_tokens"] == 2900
    assert out["usage"]["cost_usd"] == pricing.total_cost_usd([
        pricing.cost_usd("claude-haiku-5-5", 3000, 5, cache_write=2900),
        pricing.cost_usd("claude-haiku-5-5", 3100, 5, cache_read=2900)])


def test_clean_sends_the_same_system_prompt_on_every_call(monkeypatch):
    systems, out = _turn(monkeypatch)
    assert len(systems) == 2 and systems[0] == systems[1]
    assert "Request ref" not in systems[0]
    assert out["prompt_version"] == "base.v1"


def test_d28_puts_a_fresh_ref_at_the_top_of_every_call(monkeypatch):
    systems, out = _turn(monkeypatch, {"defects": "D28"})
    refs = [re.match(r"Request ref: ([0-9a-f]{12})\n\n", s) for s in systems]
    assert all(refs) and refs[0].group(1) != refs[1].group(1)
    assert all(s.count("Request ref:") == 1 for s in systems)
    assert systems[0][refs[0].end():] == systems[1][refs[1].end():]
    assert out["prompt_version"] == "base.v1+D28"
    assert prompt.REQUEST_REF in client.get(
        "/api/_test/prompt", headers={"X-Stand-Settings": json.dumps({"defects": "D28"})}).json()["text"]


def test_header_defects_do_not_leak_between_interleaved_sessions(monkeypatch):
    systems_d28, _ = _turn(monkeypatch, {"defects": "D28"})
    systems_clean, _ = _turn(monkeypatch)
    systems_d28_again, _ = _turn(monkeypatch, {"defects": "D28"})
    assert all(s.startswith("Request ref:") for s in systems_d28 + systems_d28_again)
    assert not any("Request ref" in s for s in systems_clean)


def _eligibility(enable, tx, reason, *ids):
    enable(*ids)
    from app import session_ctx
    with session_ctx.bind("CUS-0004"):
        return tools.dispatch("check_dispute_eligibility",
                              {"transaction_id": tx, "reason_code": reason})


def test_d29_hides_the_verdict_behind_terse_codes_only_when_ineligible(enable):
    clean = _eligibility(enable, "TX-0402", "duplicate_charge")
    assert clean["eligible"] is False
    terse = _eligibility(enable, "TX-0402", "duplicate_charge", "D29")
    assert terse == {"ok": True, "r": "N", "c": [3], "w": 60}
    eligible = _eligibility(enable, "TX-0401", "duplicate_charge", "D29")
    assert eligible["eligible"] is True and "checks" in eligible


def test_lesson_07_profile_is_unchanged():
    body = client.get("/api/_test/defects").json()
    assert {"D28", "D29"} <= set(body["all_defects"])
    from app import defects
    assert defects.PROFILES["lesson-07"] == ["D11", "D12", "D13"]
