""
import threading
import time
from dataclasses import dataclass

import httpx

from app import config
from app.engines import policy, reference

API_URL = "https://api.typesafe.ai/v1/systemone"
TIMEOUT_SECONDS = 1.0
BREAKER_FAILURES = 3
BREAKER_COOLDOWN_SECONDS = 60.0

INTENTS = {
    "transfer_fee": "The customer asks what a money transfer costs: the fee or tariff for SWIFT, SEPA, internal or card transfers.",
    "fx_quote": "The customer asks to convert an amount between currencies, or asks about the exchange rate, spread or final amount of a conversion.",
    "dispute_eligibility": "The customer asks whether a specific transaction (with a transaction ID or a described charge) can be disputed, or how long they have to dispute that specific transaction.",
    "dispute_policy": "The customer asks about dispute rules in general, such as how many days a dispute window lasts, without naming a specific transaction.",
    "transfer_limits": "The customer asks about daily or monthly transfer limits or how much of a limit remains.",
    "balance": "The customer asks for the balance of an account.",
    "transactions": "The customer asks to see recent transactions or transaction history.",
    "product_info": "The customer asks about a bank product, its interest rate or terms (savings accounts, cards, plans).",
    "other": "Anything else: greetings, complaints, requests outside banking support, or unclear requests.",
}

INSTRUCTIONS = ("What does the customer of a digital bank want in `customer_message`? "
                "The message may be in English or Ukrainian.")

TOOL_HINTS = {
    "fx_quote": "call `quote_fx` first",
    "dispute_eligibility": "call `check_dispute_eligibility` first",
    "transfer_limits": "call `check_limits` first",
    "balance": "call `get_account` first",
    "transactions": "call `get_account`, then `get_transactions`",
}


@dataclass(frozen=True)
class Decision:
    intent: str | None
    confidence: float
    action: str
    model: str
    input_tokens: int
    latency_ms: int
    error: str = ""


def _transfer_fee_facts() -> str:
    rows = [f"- {rail}: EUR {fee['flat_fee_eur']:g} flat + {fee['percent_fee']:g}% of the amount"
            for rail, fee in reference.transfer_fees().items()]
    return "Transfer fees (flat fee in EUR plus a percentage of the transfer amount):\n" + "\n".join(rows)


def _dispute_window_facts() -> str:
    rows = [f"- {reason}: {days} days from the transaction date"
            for reason, days in policy.DISPUTE_WINDOWS_DAYS.items()]
    return "Dispute windows by reason code:\n" + "\n".join(rows)


FACTS = {"transfer_fee": _transfer_fee_facts, "dispute_policy": _dispute_window_facts}


def block_for(intent: str | None) -> tuple[str, str]:
    if intent in FACTS:
        return ("facts", "## Verified facts from bank engines\n"
                "These values come from the bank's own engines for this request. "
                "Answer from them and state the figures exactly.\n" + FACTS[intent]())
    if intent in TOOL_HINTS:
        return ("tool_hint", "## Routing hint\nFor this request, "
                + TOOL_HINTS[intent] + ", then answer from the tool result.")
    return ("none", "")


class JevRouter:
    name = "jev"

    def __init__(self, api_key: str, model: str, min_confidence: float):
        self.api_key = api_key
        self.model = model
        self.min_confidence = min_confidence
        self._lock = threading.Lock()
        self._failures = 0
        self._open_until = 0.0

    def _breaker_open(self) -> bool:
        with self._lock:
            return time.monotonic() < self._open_until

    def _record(self, ok: bool) -> None:
        with self._lock:
            if ok:
                self._failures = 0
                return
            self._failures += 1
            if self._failures >= BREAKER_FAILURES:
                self._open_until = time.monotonic() + BREAKER_COOLDOWN_SECONDS
                self._failures = 0

    def _ask(self, message: str) -> dict:
        body = {"state": {"customer_message": message}, "model": self.model,
                "questions": {"intent": {"type": "choice", "instructions": INSTRUCTIONS,
                                         "criteria": INTENTS}}}
        r = httpx.post(API_URL, json=body, timeout=TIMEOUT_SECONDS,
                       headers={"Authorization": f"Bearer {self.api_key}"})
        r.raise_for_status()
        return r.json()

    def decide(self, message: str) -> Decision:
        if self._breaker_open():
            return Decision(None, 0.0, "none", self.model, 0, 0, "breaker open")
        started = time.perf_counter()
        try:
            data = self._ask(message)
            answer = data["answers"]["intent"]
            intent, confidence = answer["choice"], float(answer["confidence"])
        except Exception as e:
            self._record(False)
            return Decision(None, 0.0, "none", self.model, 0,
                            int((time.perf_counter() - started) * 1000),
                            f"{type(e).__name__}: {e}"[:200])
        self._record(True)
        latency = int((time.perf_counter() - started) * 1000)
        tokens = int((data.get("usage") or {}).get("input_tokens", 0))
        model = data.get("model", self.model)
        if confidence < self.min_confidence:
            return Decision(intent, confidence, "none", model, tokens, latency)
        action, _ = block_for(intent)
        return Decision(intent, confidence, action, model, tokens, latency)


_router: JevRouter | None = None


def get_router() -> JevRouter | None:
    global _router
    if config.ROUTER != "jev" or not config.TYPESAFE_API_KEY:
        return None
    if _router is None:
        _router = JevRouter(config.TYPESAFE_API_KEY, config.TYPESAFE_MODEL,
                            config.ROUTER_MIN_CONFIDENCE)
    return _router


def active_for(profile: str) -> bool:
    return config.ROUTER == "jev" and bool(config.TYPESAFE_API_KEY) \
        and profile in config.ROUTER_PROFILES


def describe() -> str:
    if config.ROUTER != "jev":
        return ""
    if not config.TYPESAFE_API_KEY:
        return "jev (no key)"
    return "jev (" + ",".join(sorted(config.ROUTER_PROFILES)) + ")"


def apply(system: str, decision: Decision) -> tuple[str, str]:
    if decision.action == "none":
        return system, ""
    _, block = block_for(decision.intent)
    return system.rstrip() + "\n\n" + block + "\n", block
