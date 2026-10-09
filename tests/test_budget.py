import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app import budget, config
from app.agent import pricing
from app.main import app

client = TestClient(app)
ASK = {"message": "What is my balance?", "customer_id": "CUS-0001"}


@pytest.fixture
def keys_on(monkeypatch):
    monkeypatch.setattr(config, "STAND_KEYS_REQUIRED", True)


@pytest.fixture
def student():
    key, raw = budget.create("test-student")
    return key, {budget.HEADER: raw}


def _ledger(key) -> list[dict]:
    conn = budget.connect()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM ledger WHERE key_id = ? ORDER BY id", (key.id,))]
    finally:
        conn.close()


def _admin(monkeypatch) -> dict:
    monkeypatch.setattr(config, "STAND_ADMIN_TOKEN", "lecturer-secret")
    return {"X-Stand-Admin": "lecturer-secret"}


def test_keys_off_leaves_chat_open():
    assert client.post("/chat", json=ASK).status_code == 200


def test_chat_without_a_key_is_401(keys_on):
    r = client.post("/chat", json=ASK)
    assert r.status_code == 401
    assert budget.HEADER in r.json()["detail"]


def test_unknown_and_revoked_keys_are_401(keys_on, student):
    assert client.post("/chat", json=ASK,
                       headers={budget.HEADER: "psk_nope"}).status_code == 401
    key, headers = student
    budget.revoke(key)
    assert client.post("/chat", json=ASK, headers=headers).status_code == 401


def test_every_spending_route_needs_a_key(keys_on):
    for path in sorted(budget_paths()):
        assert client.post(path, json={}).status_code == 401, path


def budget_paths():
    from app.main import SPENDING_PATHS
    return SPENDING_PATHS


def test_reads_stay_open(keys_on):
    for path in ("/health", "/api/_test/traces", "/api/_test/prompt", "/"):
        assert client.get(path).status_code == 200, path


def test_valid_key_charges_every_model_call(keys_on, student):
    key, headers = student
    r = client.post("/chat", json=ASK, headers=headers)
    assert r.status_code == 200
    tree = client.get(f"/api/_test/traces/{r.json()['request_id']}").json()
    calls = [c for c in tree["children"] if c["name"] == "llm.call"]
    rows = _ledger(key)
    assert len(rows) == len(calls) >= 1
    assert {row["endpoint"] for row in rows} == {"/chat"}
    assert {row["model"] for row in rows} == {"mock-1"}
    assert sum(row["input_tokens"] for row in rows) == r.json()["usage"]["input_tokens"]


def test_summary_fold_is_charged_too(keys_on, student):
    key, headers = student
    hdr = dict(headers, **{"X-Stand-Settings": json.dumps({"summarize_after": 1})})
    sid = client.post("/chat", json=ASK, headers=hdr).json()["session_id"]
    before = len(_ledger(key))
    r = client.post("/chat", json={"message": "Show transactions for ACC-1001",
                                   "session_id": sid}, headers=hdr)
    tree = client.get(f"/api/_test/traces/{r.json()['request_id']}").json()
    names = [c["name"] for c in tree["children"]]
    assert "agent.summarize" in names
    assert len(_ledger(key)) - before == names.count("llm.call") + 1


def test_priced_model_cost_lands_in_the_ledger(keys_on, student, monkeypatch):
    key, headers = student
    monkeypatch.setattr(pricing, "cost_usd", lambda m, i, o: 0.01)
    client.post("/chat", json=ASK, headers=headers)
    assert budget.usage(key)["day"]["spent_usd"] == pytest.approx(
        0.01 * len(_ledger(key)))


def test_daily_limit_returns_429(keys_on, student):
    key, headers = student
    budget.record(key, 1.0, budget.now() - timedelta(hours=1))
    r = client.post("/chat", json=ASK, headers=headers)
    assert r.status_code == 429
    assert r.json()["window"] == "day"
    assert r.json()["budget"]["day"]["oldest_charge_frees_at"]


def test_spend_older_than_a_day_counts_only_for_the_month(keys_on, student):
    key, headers = student
    budget.record(key, 1.0, budget.now() - timedelta(hours=25))
    report = budget.usage(key)
    assert report["day"]["spent_usd"] == 0
    assert report["month"]["spent_usd"] == 1.0
    assert client.post("/chat", json=ASK, headers=headers).status_code == 200


def test_monthly_limit_returns_429(keys_on, student):
    key, headers = student
    for days in (2, 5, 9, 14, 20):
        budget.record(key, 1.0, budget.now() - timedelta(days=days))
    r = client.post("/chat", json=ASK, headers=headers)
    assert r.status_code == 429
    assert r.json()["window"] == "month"


def test_spend_older_than_30_days_is_forgotten(keys_on, student):
    key, headers = student
    budget.record(key, 5.0, budget.now() - timedelta(days=31))
    assert budget.usage(key)["month"]["spent_usd"] == 0
    assert client.post("/chat", json=ASK, headers=headers).status_code == 200


def test_windows_follow_the_injected_clock(keys_on, student, monkeypatch):
    key, _ = student
    start = budget.now()
    budget.record(key, 0.6, start)
    monkeypatch.setattr(budget, "now", lambda: start + timedelta(hours=23))
    assert budget.usage(key)["day"]["spent_usd"] == 0.6
    monkeypatch.setattr(budget, "now", lambda: start + timedelta(hours=24, seconds=1))
    assert budget.usage(key)["day"]["spent_usd"] == 0


def test_per_key_limits_override_the_defaults(keys_on, student):
    key, headers = student
    budget.set_limits(key, 2.0, None)
    budget.record(key, 1.5, budget.now())
    assert client.post("/chat", json=ASK, headers=headers).status_code == 200


def test_limits_must_be_finite_and_non_negative(student):
    key, _ = student
    for bad in (float("inf"), float("nan"), -1.0):
        with pytest.raises(ValueError):
            budget.set_limits(key, bad, None)
        with pytest.raises(ValueError):
            budget.create("x", None, bad)


def test_a_key_runs_one_spending_request_at_a_time(student):
    key, _ = student
    with budget.bind(key, "/chat"):
        with pytest.raises(budget.KeyBusy):
            with budget.bind(key, "/chat"):
                pass
    with budget.bind(key, "/chat"):
        pass


def test_admin_bypasses_the_key_and_is_not_charged(keys_on, student, monkeypatch):
    key, _ = student
    r = client.post("/chat", json=ASK, headers=_admin(monkeypatch))
    assert r.status_code == 200
    assert _ledger(key) == []


def test_reset_keeps_keys_and_ledger(keys_on, student, monkeypatch):
    key, headers = student
    budget.record(key, 0.3, budget.now())
    assert client.post("/api/_test/reset", headers=_admin(monkeypatch)).status_code == 200
    assert budget.resolve(headers[budget.HEADER]) is not None
    assert budget.usage(key)["day"]["spent_usd"] == 0.3


def test_budget_endpoint_reports_the_callers_spend(student):
    key, headers = student
    budget.record(key, 0.25, budget.now())
    body = client.get("/api/_test/budget", headers=headers).json()
    assert body["label"] == "test-student"
    assert body["day"]["remaining_usd"] == 0.75
    assert body["month"]["remaining_usd"] == 4.75
    assert client.get("/api/_test/budget").status_code == 401


def test_key_list_is_lecturer_only(student, monkeypatch):
    assert client.get("/api/_test/keys").status_code == 403
    body = client.get("/api/_test/keys", headers=_admin(monkeypatch)).json()
    assert "test-student" in [k["label"] for k in body["keys"]]


def test_plaintext_key_is_not_stored(student):
    _, headers = student
    conn = budget.connect()
    try:
        dump = "\n".join(str(tuple(r)) for r in conn.execute("SELECT * FROM keys"))
    finally:
        conn.close()
    assert headers[budget.HEADER] not in dump


def test_unpriced_model_blocks_startup_when_keys_are_on(monkeypatch):
    monkeypatch.setattr(config, "STAND_KEYS_REQUIRED", True)
    monkeypatch.setattr(config, "LLM_PROVIDER", "openai")
    monkeypatch.setattr(config, "LLM_MODEL", "")
    with pytest.raises(RuntimeError, match="gpt-5-mini"):
        budget.validate_startup()
    monkeypatch.setattr(config, "LLM_PROVIDER", "anthropic")
    monkeypatch.setattr(config, "LLM_MODEL", "claude-haiku-5-5")
    budget.validate_startup()


def test_unpriced_call_at_runtime_is_charged_at_the_ceiling(student, monkeypatch):
    key, _ = student
    monkeypatch.setattr(config, "LLM_PROVIDER", "anthropic")
    with budget.bind(key, "/chat"):
        budget.charge("some-new-model", 1_000_000, 0)
    row = _ledger(key)[-1]
    assert row["priced"] == 0
    assert row["cost_usd"] == pricing.ceiling_cost_usd(1_000_000, 0) > 0


def test_compose_passes_the_key_settings_into_the_container():
    compose = (config.ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    for var in ("STAND_KEYS_REQUIRED", "STAND_KEY_DAILY_USD", "STAND_KEY_MONTHLY_USD"):
        assert f"{var}=${{{var}" in compose, var


def test_ui_sends_the_student_key():
    ui = (config.ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
    assert "h['X-Stand-Key'] = studentKey" in ui
    assert "api('/api/_test/budget')" in ui
