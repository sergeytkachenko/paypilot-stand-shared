""
import json
import re
import uuid

from app.agent.providers.base import ModelResponse, Provider

_ID_RE = {
    "account": re.compile(r"\bACC-\d{4}\b", re.I),
    "transaction": re.compile(r"\bTX-\d{4}\b", re.I),
    "email": re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),
    "amount": re.compile(r"\b(\d+(?:\.\d+)?)\s*(EUR|USD|GBP|PLN|CHF|UAH)\b", re.I),
    "reason": re.compile(r"\b(fraud_card_not_present|goods_not_received|"
                         r"duplicate_charge|service_not_rendered|unauthorized_debit)\b"),
}


def _tokens(text: str) -> int:
    return max(1, len(text) // 4)


class MockProvider(Provider):
    name = "mock"

    def complete(self, system, messages, tools):
        model = "mock-1"
        last = messages[-1]
        input_size = _tokens(system) + sum(_tokens(str(m.get("content") or "")) +
                                           _tokens(json.dumps(m.get("tool_calls") or []))
                                           for m in messages)
        if last["role"] == "tool" and last.get("name") == "get_account":
            follow = self._after_account(self._last_user_text(messages), last)
            if follow:
                return ModelResponse(tool_calls=[follow], input_tokens=input_size,
                                     output_tokens=8, model=model)
        if last["role"] == "tool":
            text = self._answer_from_tool(last)
            return ModelResponse(text=text, input_tokens=input_size,
                                 output_tokens=_tokens(text), model=model)
        call = self._route(str(last.get("content") or ""))
        if call:
            return ModelResponse(tool_calls=[call], input_tokens=input_size,
                                 output_tokens=8, model=model)
        text = ("I can help with balances, fees, limits, currency conversion "
                "and payment disputes. What would you like to do?")
        return ModelResponse(text=text, input_tokens=input_size,
                             output_tokens=_tokens(text), model=model)

    @staticmethod
    def _last_user_text(messages) -> str:
        for m in reversed(messages):
            if m["role"] == "user":
                return str(m.get("content") or "")
        return ""

    @staticmethod
    def _call(name, **arguments):
        return {"id": uuid.uuid4().hex[:12], "name": name, "arguments": arguments}

    @staticmethod
    def _needs_customer(t: str) -> str | None:
        if "human" in t or "escalate" in t:
            return "escalate"
        if "convert" in t or "fx" in t or "exchange" in t:
            return "fx"
        if "limit" in t:
            return "limits"
        return None

    def _after_account(self, text: str, tool_msg: dict) -> dict | None:
        try:
            data = json.loads(tool_msg["content"])
        except (KeyError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict) or data.get("error") or not data.get("customer"):
            return None
        cid = data["customer"]["id"]
        need = self._needs_customer(text.lower())
        if need == "escalate":
            return self._call("escalate_to_human", customer_id=cid,
                              reason="customer asked for a human")
        if need == "fx":
            m = _ID_RE["amount"].search(text)
            amount = float(m.group(1)) if m else 100.0
            frm = m.group(2).upper() if m else "EUR"
            to = "USD" if frm == "EUR" else "EUR"
            m2 = re.search(r"\b(?:to|into)\s+([A-Z]{3})\b", text, re.I)
            if m2:
                to = m2.group(1).upper()
            return self._call("quote_fx", customer_id=cid, amount=amount,
                              from_currency=frm, to_currency=to)
        if need == "limits":
            return self._call("check_limits", customer_id=cid)
        return None

    def _route(self, text: str) -> dict | None:
        t = text.lower()
        acc = _ID_RE["account"].search(text)
        tx = _ID_RE["transaction"].search(text)
        reason = _ID_RE["reason"].search(text)
        email = _ID_RE["email"].search(text)

        call = self._call

        if ("open" in t or "create" in t or "file" in t) and "dispute" in t and tx:
            return call("create_dispute", transaction_id=tx.group().upper(),
                        reason_code=(reason.group() if reason else "duplicate_charge"))
        if "dispute" in t and tx:
            return call("check_dispute_eligibility",
                        transaction_id=tx.group().upper(),
                        reason_code=(reason.group() if reason else "duplicate_charge"))
        if "statement" in t and acc and email:
            return call("send_statement", account_id=acc.group().upper(),
                        email=email.group())
        if self._needs_customer(t):
            return call("get_account")
        if ("transaction" in t or "history" in t) and acc:
            return call("get_transactions", account_id=acc.group().upper())
        if any(w in t for w in ("balance", "account", "баланс", "рахун")):
            return call("get_account")
        if any(w in t for w in ("fee", "spread", "rule", "policy", "how", "what", "why")):
            return call("search_knowledge_base", query=text[:120])
        return None

    def _answer_from_tool(self, tool_msg: dict) -> str:
        name = tool_msg.get("name", "tool")
        try:
            data = json.loads(tool_msg["content"])
        except (KeyError, json.JSONDecodeError):
            data = {}
        if isinstance(data, dict) and data.get("error"):
            return f"I could not complete that: {data['error']}"
        if name == "search_knowledge_base":
            frags = (data or {}).get("fragments", [])
            if not frags:
                return "I could not find anything relevant in the documentation."
            quoted = " | ".join(f["text"][:160] for f in frags[:2])
            return f"Here is what the documentation says: {quoted}"
        return f"Result of {name}: {json.dumps(data, ensure_ascii=False)}"
