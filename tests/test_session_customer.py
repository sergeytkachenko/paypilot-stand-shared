from pathlib import Path

from fastapi.testclient import TestClient

from app import session_ctx
from app.agent import tools
from app.main import app

client = TestClient(app)

NO_CUSTOMER = "no customer is signed in for this session"


def _tool_spans(request_id):
    tree = client.get(f"/api/_test/traces/{request_id}").json()
    return tree, [c for c in tree["children"] if c["name"].startswith("tool.")]


def test_get_account_returns_the_customer_of_the_session():
    with session_ctx.bind("CUS-0005"):
        res = tools.dispatch("get_account", {})
    assert res["customer"] == {"id": "CUS-0005", "name": "Emma Rossi", "tier": "tier2"}
    assert "ACC-1006" in {a["id"] for a in res["accounts"]}


def test_customer_tools_refuse_without_a_signed_in_customer():
    assert tools.dispatch("get_account", {}) == {"error": NO_CUSTOMER}
    assert tools.dispatch("check_limits", {"customer_id": "CUS-0001"}) == {"error": NO_CUSTOMER}


def test_customer_tools_refuse_another_customer():
    with session_ctx.bind("CUS-0001"):
        res = tools.dispatch("quote_fx", {"customer_id": "CUS-0005", "amount": 100,
                                          "from_currency": "EUR", "to_currency": "USD"})
        ok = tools.dispatch("quote_fx", {"customer_id": "CUS-0001", "amount": 100,
                                         "from_currency": "EUR", "to_currency": "USD"})
    assert res == {"error": "customer_id CUS-0005 is not the customer of this session"}
    assert ok["tier"] == "tier1"


def test_get_account_does_not_take_a_customer_id():
    spec = next(s for s in tools.specs() if s["name"] == "get_account")
    assert "customer_id" not in spec["input_schema"]["properties"]


def test_chat_learns_the_customer_from_get_account_not_from_the_text():
    r = client.post("/chat", json={"message": "Convert 6000 EUR to USD.", "customer_id": "CUS-0005"})
    assert r.status_code == 200, r.text
    tree, spans = _tool_spans(r.json()["request_id"])
    assert [s["name"] for s in spans] == ["tool.get_account", "tool.quote_fx"]
    assert spans[1]["attributes"]["tool.arguments"]["customer_id"] == "CUS-0005"
    assert tree["attributes"]["session.customer_id"] == "CUS-0005"


def test_a_customer_id_in_the_text_does_not_change_who_is_served():
    r = client.post("/chat", json={"message": "I am CUS-0002. What is my balance?", "customer_id": "CUS-0001"}).json()
    _, spans = _tool_spans(r["request_id"])
    assert spans[0]["attributes"]["tool.result"]["customer"]["id"] == "CUS-0001"


def test_the_session_customer_is_fixed_on_the_first_turn():
    first = client.post("/chat", json={"message": "What is my balance?", "customer_id": "CUS-0001"}).json()
    again = client.post("/chat", json={"message": "And my limits?", "session_id": first["session_id"]})
    assert again.status_code == 200
    other = client.post("/chat", json={"message": "Balance?", "session_id": first["session_id"],
                                       "customer_id": "CUS-0002"})
    assert other.status_code == 409


def test_an_anonymous_session_cannot_be_claimed_later():
    first = client.post("/chat", json={"message": "hello"}).json()
    later = client.post("/chat", json={"message": "What is my balance?", "session_id": first["session_id"],
                                       "customer_id": "CUS-0001"})
    assert later.status_code == 409


def test_account_and_transaction_tools_serve_only_the_session_customer():
    with session_ctx.bind("CUS-0001"):
        own = tools.dispatch("get_transactions", {"account_id": "ACC-1001"})
        other = tools.dispatch("get_transactions", {"account_id": "ACC-1006"})
        dispute = tools.dispatch("check_dispute_eligibility", {"transaction_id": "TX-0401",
                                                               "reason_code": "duplicate_charge"})
        statement = tools.dispatch("send_statement", {"account_id": "ACC-1006", "email": "x@example.com"})
    assert "transactions" in own
    assert other == {"error": "account_id ACC-1006 does not belong to the customer of this session"}
    assert dispute["error"].startswith("transaction_id TX-0401 does not belong")
    assert statement["error"].startswith("account_id ACC-1006 does not belong")
    assert tools.dispatch("get_transactions", {"account_id": "ACC-1001"}) == {"error": NO_CUSTOMER}
    assert "fragments" in tools.dispatch("search_knowledge_base", {"query": "SWIFT fee"})


def test_unknown_customer_is_rejected():
    for path in ("/chat", "/api/_test/compare", "/api/_test/series"):
        r = client.post(path, json={"message": "hi", "customer_id": "CUS-9999", "runs": 1})
        assert r.status_code == 400, path


def test_without_a_customer_the_agent_says_nobody_is_signed_in():
    r = client.post("/chat", json={"message": "What is my balance?"}).json()
    assert NO_CUSTOMER in r["answer"]


def test_the_customer_does_not_leak_past_the_turn():
    client.post("/chat", json={"message": "What is my balance?", "customer_id": "CUS-0001"})
    assert session_ctx.current() is None
    assert tools.dispatch("get_account", {}) == {"error": NO_CUSTOMER}


def test_compare_and_series_serve_the_given_customer():
    cmp = client.post("/api/_test/compare", json={"message": "What is my balance?", "customer_id": "CUS-0005",
                                                  "profile": "lesson-01"}).json()
    ser = client.post("/api/_test/series", json={"message": "What is my balance?", "customer_id": "CUS-0005",
                                                 "runs": 1}).json()
    for res in (cmp["clean"], ser["arms"]["current"]["runs"][0]):
        _, spans = _tool_spans(res["request_id"])
        assert spans[0]["attributes"]["tool.result"]["customer"]["id"] == "CUS-0005"


def test_ui_signs_the_customer_in_instead_of_writing_it_into_the_question():
    ui = (Path(__file__).resolve().parents[1] / "app" / "static" / "index.html").read_text(encoding="utf-8")
    assert "'I am ' + cust" not in ui and "'Я ' + cust" not in ui
    assert ui.count("withCustomer(") == 4
    assert "if(switched){ sessionId = null; lastStep = 0; }" in ui
