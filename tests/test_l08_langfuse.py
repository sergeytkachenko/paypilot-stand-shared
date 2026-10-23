import base64
import json

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app import config, otel, runctx
from app.agent import loop, pricing
from app.agent.providers import mock, openai_provider, openrouter_provider


@pytest.fixture
def capture(monkeypatch):
    monkeypatch.setattr(config, "TRACE_CAPTURE_CONTENT", True)


@pytest.fixture
def priced_mock(monkeypatch):
    original = mock.MockProvider.complete

    def complete(self, system, messages, tools):
        resp = original(self, system, messages, tools)
        resp.model = "anthropic/claude-haiku-5.5"
        resp.provider_cost_usd = pricing.cost_usd(resp.model, resp.input_tokens,
                                                  resp.output_tokens)
        return resp

    monkeypatch.setattr(mock.MockProvider, "complete", complete)


def _spans(tree, name):
    return [c for c in tree["children"] if c["name"] == name]


def _tree(result):
    from app import tracing
    return tracing.get(result["request_id"])


def test_usage_buckets_are_exclusive_and_add_up():
    attrs = {"gen_ai.usage.input_tokens": 5000, "gen_ai.usage.cache_read_input_tokens": 3000,
             "gen_ai.usage.cache_creation_input_tokens": 500, "gen_ai.usage.output_tokens": 120,
             "gen_ai.usage.reasoning_tokens": 40}
    buckets = otel.usage_details(attrs)
    assert buckets == {"input": 1500, "input_cached_tokens": 3000,
                       "input_cache_creation_tokens": 500, "output": 80,
                       "output_reasoning_tokens": 40}
    assert (buckets["input"] + buckets["input_cached_tokens"]
            + buckets["input_cache_creation_tokens"]) == 5000
    assert buckets["output"] + buckets["output_reasoning_tokens"] == 120


@pytest.mark.parametrize("name, kind", [
    ("agent.request", "agent"), ("llm.call", "generation"), ("agent.summarize", "generation"),
    ("router.jev", "generation"), ("tool.search_knowledge_base", "retriever"),
    ("tool.get_transactions", "tool"), ("something.else", "span")])
def test_observation_type_follows_the_span_name(name, kind):
    assert otel.observation_type(name) == kind


def test_trace_attributes_land_on_every_span_and_cost_stays_out_of_gen_ai(enable, monkeypatch):
    monkeypatch.setattr(config, "TRACE_TAG", "student-7")
    monkeypatch.setattr(config, "STAND_RELEASE", "abc1234")
    enable("D14")
    tree = _tree(loop.run_turn(None, "Show transactions for ACC-1004", "CUS-0003"))
    for node in [tree, *tree["children"]]:
        attrs = otel.langfuse_attributes(node, tree)
        assert attrs["session.id"] == tree["attributes"]["session.id"]
        assert attrs["user.id"] == "CUS-0003"
        assert attrs["langfuse.environment"] == "clean"
        assert attrs["langfuse.release"] == "abc1234"
        assert {"clean", "D14", "mock-1", "student-7"} <= set(attrs["langfuse.trace.tags"])
        assert "gen_ai.usage.cost" not in attrs
    root = otel.langfuse_attributes(tree, tree)
    assert root["langfuse.observation.type"] == "agent"
    assert root["langfuse.trace.metadata.step"] == 1
    call = otel.langfuse_attributes(_spans(tree, "llm.call")[0], tree)
    assert call["langfuse.observation.prompt.name"] == "base"
    assert call["langfuse.observation.prompt.version"] == 1
    assert json.loads(call["langfuse.observation.usage_details"])["input"] >= 0


def test_stand_cost_is_written_for_a_priced_model():
    tree = {"attributes": {"run.profile": "clean"}, "children": []}
    node = {"name": "llm.call", "attributes": {
        "gen_ai.request.model": "anthropic/claude-haiku-5.5",
        "gen_ai.usage.input_tokens": 1000, "gen_ai.usage.output_tokens": 100}}
    attrs = otel.langfuse_attributes(node, tree)
    assert attrs["stand.cost_usd"] == pricing.cost_usd("anthropic/claude-haiku-5.5", 1000, 100)


def test_only_the_root_gets_the_dialog_input_and_output():
    tree = {"name": "agent.request", "attributes": {"request.message": "hi", "response.answer": "hello"},
            "children": []}
    child = {"name": "llm.call", "attributes": {}}
    assert "langfuse.observation.input" not in otel.langfuse_attributes(child, tree)
    assert otel.langfuse_attributes(tree, tree)["langfuse.observation.input"] == "hi"


def test_prompt_overlays_go_to_metadata():
    tree = {"attributes": {"prompt.version": "base.v1+D05"}, "children": []}
    attrs = otel.langfuse_attributes({"name": "llm.call", "attributes": {}}, tree)
    assert attrs["langfuse.observation.prompt.version"] == 1
    assert attrs["langfuse.observation.metadata.prompt_version"] == "base.v1+D05"


def test_export_keeps_real_timestamps(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(otel, "_tracer", provider.get_tracer("test"))
    monkeypatch.setattr(otel, "_enabled", True)
    tree = {"name": "agent.request", "span_id": "r", "started_at": 100.0, "ended_at": 103.5,
            "attributes": {"session.id": "s1", "run.profile": "lesson-08"},
            "children": [{"name": "llm.call", "span_id": "c", "started_at": 100.5,
                          "ended_at": 102.0, "attributes": {}, "children": []}]}
    otel.export_tree(tree)
    spans = {s.name: s for s in exporter.get_finished_spans()}
    assert spans["agent.request"].start_time == 100_000_000_000
    assert spans["agent.request"].end_time == 103_500_000_000
    assert spans["llm.call"].end_time - spans["llm.call"].start_time == 1_500_000_000
    assert spans["llm.call"].attributes["session.id"] == "s1"
    assert spans["llm.call"].attributes["langfuse.environment"] == "lesson-08"
    assert spans["llm.call"].parent.span_id == spans["agent.request"].context.span_id


def test_root_totals_do_not_read_as_usage_in_the_backend():
    out = otel.exported_attributes({"gen_ai.usage.total_input_tokens": 10,
                                    "gen_ai.usage.input_tokens": 5, "x": [1]})
    assert out == {"stand.usage.total_input_tokens": 10, "gen_ai.usage.input_tokens": 5,
                   "x": "[1]"}


def test_init_leaves_the_global_tracer_provider_alone(monkeypatch):
    from opentelemetry import trace
    before = trace.get_tracer_provider()
    monkeypatch.setattr(config, "LANGFUSE_HOST", "http://127.0.0.1:9")
    monkeypatch.setattr(config, "LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setattr(config, "LANGFUSE_SECRET_KEY", "sk")
    monkeypatch.setattr(otel, "_tracer", None)
    monkeypatch.setattr(otel, "_enabled", False)
    monkeypatch.setattr(otel, "_backends", [])
    assert otel.init()
    assert otel.backends() == ["langfuse"]
    assert trace.get_tracer_provider() is before


def test_langfuse_target_builds_basic_auth(monkeypatch):
    monkeypatch.setattr(config, "LANGFUSE_HOST", "http://langfuse-web:3000")
    monkeypatch.setattr(config, "LANGFUSE_PUBLIC_KEY", "pk-lf-x")
    monkeypatch.setattr(config, "LANGFUSE_SECRET_KEY", "sk-lf-y")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://phoenix:6006")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "a=1, b=two")
    found = {name: (url, headers) for name, url, headers in otel.targets()}
    assert found["otlp"] == ("http://phoenix:6006/v1/traces", {"a": "1", "b": "two"})
    url, headers = found["langfuse"]
    assert url == "http://langfuse-web:3000/api/public/otel/v1/traces"
    assert headers["Authorization"] == "Basic " + base64.b64encode(b"pk-lf-x:sk-lf-y").decode()
    assert headers["x-langfuse-ingestion-version"] == "4"


def test_content_is_off_by_default():
    tree = _tree(loop.run_turn(None, "What is my balance?", "CUS-0001"))
    assert "gen_ai.prompt" not in _spans(tree, "llm.call")[0]["attributes"]
    assert "request.message" not in tree["attributes"]


def test_prompt_shows_the_d15_context_replay(capture, enable):
    enable("D15")
    first = loop.run_turn(None, "What is my balance?", "CUS-0001")
    tree = _tree(loop.run_turn(first["session_id"], "Show transactions for ACC-1001"))
    sent = json.loads(_spans(tree, "llm.call")[0]["attributes"]["gen_ai.prompt"])
    assert sent["messages"][0]["role"] == "system"
    assert "(context replay)" in json.dumps(sent["messages"][1:])
    assert {"type": "function", "function": {"name": "get_transactions"}} in sent["tools"]
    answer = json.loads(_spans(tree, "llm.call")[0]["attributes"]["gen_ai.completion"])
    assert answer["role"] == "assistant"
    assert answer["tool_calls"][0]["function"]["name"] == "get_transactions"
    assert tree["attributes"]["request.message"] == "Show transactions for ACC-1001"
    assert tree["attributes"]["response.answer"]


def test_retries_carry_their_prompt_too(capture, enable):
    enable("D14")
    tree = _tree(loop.run_turn(None, "Show transactions for ACC-1004", "CUS-0003"))
    retries = [c for c in _spans(tree, "llm.call") if "retry.attempt" in c["attributes"]]
    assert retries
    assert all("gen_ai.prompt" in r["attributes"] for r in retries)


def test_summarize_keeps_its_input_and_tokens(capture):
    with runctx.override(summarize_after=1):
        first = loop.run_turn(None, "Is TX-0401 eligible for a dispute, duplicate_charge?",
                              "CUS-0004")
        tree = _tree(loop.run_turn(first["session_id"], "What documents do I need?"))
    span = _spans(tree, "agent.summarize")[0]["attributes"]
    assert "TX-0401" in span["summary.input"]
    assert span["gen_ai.usage.input_tokens"] > 0
    attrs = otel.langfuse_attributes(_spans(tree, "agent.summarize")[0], tree)
    assert attrs["langfuse.observation.input"] == span["summary.input"]
    assert attrs["langfuse.observation.output"] == span["summary.text"]


def test_provider_cost_sees_the_d14_retries_the_stand_field_misses(enable, priced_mock):
    enable("D14")
    usage = loop.run_turn(None, "Show transactions for ACC-1004", "CUS-0003")["usage"]
    assert usage["cost_usd"] is not None
    assert usage["provider_cost_usd"] > usage["cost_usd"] * 3


def test_provider_cost_is_none_without_a_provider_bill():
    usage = loop.run_turn(None, "What is my balance?", "CUS-0001")["usage"]
    assert usage["provider_cost_usd"] is None


class _ReasoningResp:
    def raise_for_status(self):
        pass

    def json(self):
        return {"model": "anthropic/claude-haiku-5.5",
                "choices": [{"message": {
                    "content": "17 × 23 = 391", "reasoning": "17*20 + 17*3 = 391",
                    "reasoning_details": [{"type": "reasoning.text", "text": "17*20 + 17*3 = 391",
                                           "format": "anthropic-claude-v1", "index": 0,
                                           "signature": "sig"}]}}],
                "usage": {"prompt_tokens": 27, "completion_tokens": 140, "cost": 7.27e-05,
                          "prompt_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
                          "completion_tokens_details": {"reasoning_tokens": 71}}}


def test_openrouter_cost_and_reasoning_are_parsed(monkeypatch):
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setattr(openai_provider.httpx, "post", lambda *a, **k: _ReasoningResp())
    resp = openrouter_provider.OpenRouterProvider().complete(
        "sys", [{"role": "user", "content": "17*23?"}], [])
    assert resp.provider_cost_usd == 7.27e-05
    assert resp.reasoning_tokens == 71
    assert resp.reasoning_text == "17*20 + 17*3 = 391"
    assert resp.output_tokens == 140


def test_reasoning_details_are_the_fallback():
    msg = {"reasoning_details": [{"type": "reasoning.text", "text": "a"},
                                 {"type": "reasoning.summary", "summary": "b"}]}
    assert openai_provider._reasoning_text(msg) == "ab"
    assert openai_provider._reasoning_text({}) is None
