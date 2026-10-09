import json
import threading

import pytest
from fastapi.testclient import TestClient

from app import budget, config, db, workspace
from app.main import app

client = TestClient(app)
ASK = {"message": "What is my balance?", "customer_id": "CUS-0001"}


@pytest.fixture(autouse=True)
def shared_stand(monkeypatch):
    monkeypatch.setattr(config, "STAND_KEYS_REQUIRED", True)
    monkeypatch.setattr(config, "STAND_LOCK_GLOBAL", True)
    monkeypatch.setattr(config, "STAND_ADMIN_TOKEN", "lecturer-secret")


def _key(label: str) -> dict:
    _, raw = budget.create(label)
    return {budget.HEADER: raw}


@pytest.fixture
def a():
    return _key("student-a")


@pytest.fixture
def b():
    return _key("student-b")


ADMIN = {"X-Stand-Admin": "lecturer-secret"}


def _profile(headers=None) -> str:
    return client.get("/api/_test/defects", headers=headers or {}).json()["profile"]


def test_profile_switch_stays_inside_the_key(a, b):
    r = client.put("/api/_test/profile", json={"profile": "lesson-04"}, headers=a)
    assert r.status_code == 200
    assert r.json()["profile"] == "lesson-04"
    assert r.json()["scope"] == "key"
    assert _profile(a) == "lesson-04"
    assert _profile(b) == "clean"
    assert _profile() == "clean"


def test_header_settings_win_over_key_settings(a):
    client.put("/api/_test/profile", json={"profile": "lesson-04"}, headers=a)
    hdr = dict(a, **{"X-Stand-Settings": json.dumps({"profile": "lesson-02"})})
    body = client.get("/api/_test/defects", headers=hdr).json()
    assert body["profile"] == "lesson-02"
    assert body["scope"] == "request"


def test_profile_none_returns_to_the_server_profile(a):
    client.put("/api/_test/profile", json={"profile": "lesson-04"}, headers=a)
    client.put("/api/_test/profile", json={"profile": None}, headers=a)
    assert _profile(a) == "clean"


def test_defects_clock_retrieval_and_fold_are_per_key(a, b):
    assert client.put("/api/_test/defects", json={"defects": "D16"}, headers=a).json()["extra_defects"] == ["D16"]
    assert client.post("/api/_test/clock", json={"now": "2026-01-01T00:00:00Z"},
                       headers=a).json()["now"].startswith("2026-01-01")
    ret = client.put("/api/_test/retrieval", json={"top_k": 3, "index": "kb_broken"},
                     headers=a).json()
    assert (ret["top_k"], ret["index"]) == (3, "kb_broken")
    assert client.put("/api/_test/summarize_after", json={"steps": 2},
                      headers=a).json()["summarize_after_steps"] == 2

    for headers in (b, {}):
        assert client.get("/api/_test/defects", headers=headers).json()["extra_defects"] == []
        assert client.get("/api/_test/clock", headers=headers).json()["now"].startswith("2026-09-15")
        assert client.get("/api/_test/retrieval", headers=headers).json()["top_k"] == 4
        assert client.get("/api/_test/summarize_after",
                          headers=headers).json()["summarize_after_steps"] == 8


def test_key_writes_are_validated(a):
    assert client.put("/api/_test/profile", json={"profile": "nope"}, headers=a).status_code == 400
    assert client.put("/api/_test/defects", json={"defects": "D999"}, headers=a).status_code == 400
    assert client.post("/api/_test/clock", json={"now": "yesterday"}, headers=a).status_code == 400
    assert client.put("/api/_test/retrieval", json={"top_k": 50}, headers=a).status_code == 400
    assert client.put("/api/_test/summarize_after", json={"steps": 0}, headers=a).status_code == 400
    assert workspace.settings(budget.resolve(a[budget.HEADER])).is_empty()


def _insert_dispute(headers: dict) -> None:
    key = budget.resolve(headers[budget.HEADER])
    workspace.ensure(key)
    with db.bound(workspace.db_path(key)):
        db.execute("INSERT INTO disputes (transaction_id, account_id, reason_code, "
                   "amount, currency, created_at) VALUES ('TX-1', 'ACC-1001', 'fraud', "
                   "10, 'EUR', '2026-09-15')")


def _disputes(headers=None) -> list:
    return client.get("/api/_test/state/disputes", headers=headers or {}).json()["rows"]


def test_each_key_has_its_own_database_and_reset_is_scoped(a, b):
    _insert_dispute(a)
    assert len(_disputes(a)) == 1
    assert _disputes(b) == []
    assert _disputes() == []

    _insert_dispute(b)
    r = client.post("/api/_test/reset", headers=a)
    assert r.status_code == 200
    assert r.json()["scope"] == "key"
    assert _disputes(a) == []
    assert len(_disputes(b)) == 1


def test_reset_with_a_key_keeps_other_sessions(a, b):
    for headers in (a, b):
        client.post("/chat", json=dict(ASK, session_id="same-id"), headers=headers)
    client.post("/api/_test/reset", headers=a)
    after_a = client.post("/chat", json=dict(ASK, session_id="same-id"), headers=a).json()
    after_b = client.post("/chat", json=dict(ASK, session_id="same-id"), headers=b).json()
    assert after_a["step_number"] == 1
    assert after_b["step_number"] == 2


def test_same_session_id_is_two_conversations(a, b):
    first = client.post("/chat", json=dict(ASK, session_id="s1"), headers=a).json()
    other = client.post("/chat", json=dict(ASK, session_id="s1"), headers=b).json()
    assert first["step_number"] == other["step_number"] == 1


def test_without_a_key_server_writes_stay_locked():
    assert client.put("/api/_test/profile", json={"profile": "lesson-04"}).status_code == 403
    assert client.post("/api/_test/reset").status_code == 403


def test_lecturer_still_acts_globally(a, b):
    from app import defects
    try:
        r = client.put("/api/_test/profile", json={"profile": "lesson-04"},
                       headers=dict(a, **ADMIN))
        assert r.status_code == 200
        assert _profile() == "lesson-04"
        assert _profile(b) == "lesson-04"
        assert workspace.settings(budget.resolve(a[budget.HEADER])).is_empty()
    finally:
        defects.set_runtime_profile(None)


def test_invalid_key_is_401_even_on_reads():
    assert client.get("/health", headers={budget.HEADER: "psk_nope"}).status_code == 401
    assert client.get("/health").status_code == 200


def test_invalid_key_wins_over_a_malformed_settings_header():
    r = client.get("/health", headers={budget.HEADER: "psk_nope",
                                       "X-Stand-Settings": "{bad"})
    assert r.status_code == 401


def test_concurrent_writes_of_one_key_keep_every_field(a):
    key = budget.resolve(a[budget.HEADER])
    fields_to_set = [{"profile": "lesson-04"}, {"defects": "D16"}, {"top_k": 3},
                     {"summarize_after": 2}, {"clock": "2026-01-01T00:00:00Z"}]
    threads = [threading.Thread(target=workspace.update, args=(key,), kwargs=f)
               for f in fields_to_set * 4]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stored = workspace.settings(key).as_dict()
    for f in fields_to_set:
        assert stored.items() >= f.items()


def test_health_shows_only_the_callers_key(a):
    client.put("/api/_test/profile", json={"profile": "lesson-04"}, headers=a)
    mine = client.get("/health", headers=a).json()
    assert mine["key"] == a[budget.HEADER][:12]
    assert mine["key_settings"] == {"profile": "lesson-04"}
    anon = client.get("/health").json()
    assert anon["key"] is None and anon["key_settings"] is None


def test_keys_off_keeps_everything_global(monkeypatch, a):
    from app import defects
    monkeypatch.setattr(config, "STAND_KEYS_REQUIRED", False)
    monkeypatch.setattr(config, "STAND_LOCK_GLOBAL", False)
    try:
        client.put("/api/_test/profile", json={"profile": "lesson-04"}, headers=a)
        assert _profile() == "lesson-04"
    finally:
        defects.set_runtime_profile(None)


def test_key_settings_survive_a_restart(a):
    client.put("/api/_test/profile", json={"profile": "lesson-04"}, headers=a)
    key = budget.resolve(a[budget.HEADER])
    assert workspace.settings(key).profile == "lesson-04"


def test_revoke_drops_the_workspace(a):
    key = budget.resolve(a[budget.HEADER])
    client.put("/api/_test/profile", json={"profile": "lesson-04"}, headers=a)
    _insert_dispute(a)
    assert workspace.drop(key) is True
    assert not workspace.db_path(key).exists()
    assert workspace.settings(key).is_empty()


def test_concurrent_first_requests_seed_once_and_stay_isolated(a, b):
    errors: list[Exception] = []

    def hit(headers):
        try:
            r = client.get("/api/_test/seed", headers=headers)
            assert r.status_code == 200
            assert len(r.json()["customers"]) > 0
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=hit, args=(h,)) for h in [a] * 8 + [b] * 8]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    _insert_dispute(a)
    assert _disputes(b) == []
