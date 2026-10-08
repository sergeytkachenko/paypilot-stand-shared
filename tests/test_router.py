import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import config
from app.agent import loop, router
from app.agent.providers.base import ModelResponse
from app.main import app

client = TestClient(app)


def _answer(intent: str, confidence: float) -> dict:
    return {"model": "jev-1.13.0",
            "answers": {"intent": {"type": "choice", "choice": intent,
                                   "probabilities": {intent: confidence},
                                   "confidence": confidence}},
            "usage": {"input_tokens": 400, "output_tokens": 20}}


@pytest.fixture
def jev(monkeypatch):
    monkeypatch.setattr(config, "ROUTER", "jev")
    monkeypatch.setattr(config, "TYPESAFE_API_KEY", "test-key")
    monkeypatch.setattr(config, "ROUTER_PROFILES", {"clean"})
    monkeypatch.setattr(router, "_router", None)
    asked = []

    def install(reply):
        def fake_ask(self, message):
            asked.append(message)
            if isinstance(reply, Exception):
                raise reply
            return reply
        monkeypatch.setattr(router.JevRouter, "_ask", fake_ask)
        return asked
    return install


@pytest.fixture
def systems(monkeypatch):
    seen = []

    class Recorder:
        name = "anthropic"
        model = "agent-model"

        def complete(self, system, messages, tools):
            seen.append(system)
            return ModelResponse(text="ok", input_tokens=5, output_tokens=1, model=self.model)

    monkeypatch.setattr(loop, "get_provider", Recorder)
    return seen


def _hdr(profile: str) -> dict:
    return {"X-Stand-Settings": json.dumps({"profile": profile})}


def _router_span(request_id: str) -> dict | None:
    tree = client.get(f"/api/_test/traces/{request_id}").json()
    return next((c for c in tree["children"] if c["name"] == "router.jev"), None)


def test_confident_fee_question_gets_engine_tariffs(jev, systems):
    jev(_answer("transfer_fee", 0.97))
    r = client.post("/chat", json={"message": "Які тарифи на SWIFT-перекази?"},
                    headers=_hdr("clean")).json()
    assert "Verified facts from bank engines" in systems[0]
    assert "swift: EUR 15 flat + 0.3%" in systems[0]
    span = _router_span(r["request_id"])
    assert span["attributes"]["router.intent"] == "transfer_fee"
    assert span["attributes"]["router.action"] == "facts"
    assert r["prompt_version"] == "base.v1"


def test_confident_fx_question_gets_a_tool_hint(jev, systems):
    jev(_answer("fx_quote", 0.95))
    client.post("/chat", json={"message": "Convert 6000 EUR to USD"}, headers=_hdr("clean"))
    assert "call `quote_fx` first" in systems[0]


def test_low_confidence_changes_nothing(jev, systems):
    jev(_answer("dispute_eligibility", 0.55))
    r = client.post("/chat", json={"message": "How many days to dispute?"},
                    headers=_hdr("clean")).json()
    assert "Routing hint" not in systems[0] and "Verified facts" not in systems[0]
    assert _router_span(r["request_id"])["attributes"]["router.action"] == "none"


@pytest.mark.parametrize("failure", [httpx.ConnectTimeout("slow"),
                                     httpx.HTTPStatusError("429", request=httpx.Request("POST", "x"),
                                                           response=httpx.Response(429))])
def test_jev_failure_falls_back_to_the_plain_turn(jev, systems, failure):
    jev(failure)
    r = client.post("/chat", json={"message": "SWIFT fee?"}, headers=_hdr("clean")).json()
    assert r["answer"] == "ok"
    assert "Verified facts" not in systems[0]
    assert _router_span(r["request_id"])["attributes"]["router.error"]


def test_breaker_skips_jev_after_three_failures(jev, systems):
    asked = jev(httpx.ConnectTimeout("slow"))
    for _ in range(5):
        client.post("/chat", json={"message": "SWIFT fee?"}, headers=_hdr("clean"))
    assert len(asked) == 3


def test_lesson_profiles_and_later_turns_are_not_routed(jev, systems):
    asked = jev(_answer("transfer_fee", 0.97))
    r = client.post("/chat", json={"message": "SWIFT fee?"}, headers=_hdr("lesson-02")).json()
    assert _router_span(r["request_id"]) is None
    first = client.post("/chat", json={"message": "SWIFT fee?"}, headers=_hdr("clean")).json()
    second = client.post("/chat", json={"message": "And SEPA?", "session_id": first["session_id"]},
                         headers=_hdr("clean")).json()
    assert _router_span(second["request_id"]) is None
    assert asked == ["SWIFT fee?"]


def test_router_off_without_key_or_flag(monkeypatch, systems):
    monkeypatch.setattr(router, "_router", None)
    monkeypatch.setattr(config, "ROUTER", "jev")
    monkeypatch.setattr(config, "TYPESAFE_API_KEY", "")
    r = client.post("/chat", json={"message": "SWIFT fee?"}, headers=_hdr("clean")).json()
    assert _router_span(r["request_id"]) is None
    monkeypatch.setattr(config, "ROUTER", "")
    monkeypatch.setattr(config, "TYPESAFE_API_KEY", "k")
    r = client.post("/chat", json={"message": "SWIFT fee?"}, headers=_hdr("clean")).json()
    assert _router_span(r["request_id"]) is None
    assert client.get("/health").json()["router"] == ""


def test_router_cost_and_visibility(jev, systems, monkeypatch):
    from app.agent import pricing
    monkeypatch.setitem(pricing.PRICES_PER_MTOK_USD, "agent-model", (1.0, 5.0))
    jev(_answer("transfer_fee", 0.97))
    r = client.post("/chat", json={"message": "SWIFT fee?"}, headers=_hdr("clean")).json()
    assert r["usage"]["cost_usd"] == round((5 * 1.0 + 1 * 5.0) / 1e6 + 400 * 0.042 / 1e6, 6)
    assert client.get("/health").json()["router"] == "jev (clean)"
    d = client.post("/api/_test/compare",
                    json={"message": "SWIFT fee?", "profile": "lesson-02"}).json()
    assert d["clean"]["router"] == "jev (clean)" and d["profile"]["router"] == ""
    assert "Verified facts" not in client.get("/api/_test/prompt").json()["text"]
