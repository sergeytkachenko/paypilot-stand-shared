""
import json
import re

from app import db, tracing
from app.engines import fx as fx_engine
from app.engines import policy

_SYMBOLS = {"€": "EUR", "$": "USD", "£": "GBP"}
_CODES = "|".join(sorted(policy.RATES_TO_EUR))
_SEP = "\u00a0\u202f "
_NUM = rf"\d{{1,3}}(?:[,{_SEP}]\d{{3}})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?"
_VALUE_RE = re.compile(
    rf"(?:(?P<pre>{_CODES}|[€$£])\s?)?(?P<num>{_NUM})(?:\s?(?P<post>{_CODES}|%))?")
_ANY_NUM_RE = re.compile(_NUM)
_NAMED = {"final_amount": "receives", "spread_pct": "spread",
          "daily_remaining_eur": "dailyLeft", "monthly_remaining_eur": "monthlyLeft"}


def calls_in(tree: dict) -> list[dict]:
    out = []

    def walk(node):
        name = node.get("name") or ""
        if name.startswith("tool."):
            attrs = node.get("attributes") or {}
            out.append({"name": name[len("tool."):],
                        "arguments": attrs.get("tool.arguments") or {},
                        "result": attrs.get("tool.result")})
        for child in node.get("children") or []:
            walk(child)

    walk(tree or {})
    return out


def tool_calls(request_id: str) -> list[dict]:
    return calls_in(tracing.get(request_id))


def _number(text: str) -> float:
    digits = re.sub(f"[{_SEP}]", "", text)
    if "," in digits and "." in digits:
        decimal = "," if digits.rindex(",") > digits.rindex(".") else "."
        digits = digits.replace("." if decimal == "," else ",", "").replace(",", ".")
    elif "," in digits:
        digits = digits.replace(",", "" if re.fullmatch(r"\d{1,3}(?:,\d{3})+", digits) else ".")
    return round(float(digits), 2)


def _question_numbers(message: str) -> set[float]:
    return {_number(m) for m in _ANY_NUM_RE.findall(message or "")}


def reply_values(answer: str, message: str) -> list[dict]:
    asked = _question_numbers(message)
    out = []
    for m in _VALUE_RE.finditer(answer or ""):
        unit = m.group("post") or m.group("pre")
        if not unit:
            continue
        unit = _SYMBOLS.get(unit, unit)
        value = _number(m.group("num"))
        if value in asked:
            continue
        item = {"unit": unit, "value": value}
        if item not in out:
            out.append(item)
    return out


def _ok_results(calls: list[dict], name: str) -> list[dict]:
    return [c for c in calls if c["name"] == name and isinstance(c["result"], dict)
            and "error" not in c["result"]]


def _fx_values(quote: dict, stated: list[dict]) -> list[dict]:
    target = quote.get("to_currency")
    amounts = [v for v in stated if v["unit"] == target]
    pcts = [v for v in stated if v["unit"] == "%"]
    tool_final = round(float(quote.get("final_amount") or 0), 2)
    tool_spread = round(float(quote.get("spread_pct") or 0), 2)
    near = [v["value"] for v in amounts if abs(v["value"] - tool_final) <= 0.2 * tool_final]
    receives = tool_final if tool_final in near or not near else min(near, key=lambda x: abs(x - tool_final))
    rates = [v["value"] for v in pcts if v["value"] < 10]
    spread = tool_spread if tool_spread in rates or not rates else rates[0]
    return [{"kind": "receives", "unit": target, "value": receives},
            {"kind": "spread", "unit": "%", "value": spread}]


def _customer_tier(customer_id) -> str | None:
    row = db.one("SELECT tier FROM customers WHERE id = ?", (customer_id,)) if customer_id else None
    return row["tier"] if row else None


def _fx_reference(call: dict, values: list[dict]) -> dict | None:
    quote = call["result"]
    tier = _customer_tier(call["arguments"].get("customer_id")) or quote.get("tier")
    if tier not in policy.TIERS:
        return None
    try:
        want = fx_engine.quote(float(quote["amount"]), quote["from_currency"],
                               quote["to_currency"], tier,
                               allowance_used_eur=float(quote.get("allowance_used_before_eur") or 0))
    except (KeyError, TypeError, ValueError):
        return None
    got = {v["kind"]: v["value"] for v in values}
    ok = (abs(got.get("receives", 0) - want.final_amount) < 0.01
          and abs(got.get("spread", 0) - want.spread_pct) < 0.001)
    return {"tier": tier, "ok": ok, "spread_pct": round(want.spread_pct, 2),
            "final_amount": round(want.final_amount, 2)}


def _named(calls: list[dict], stated: list[dict]) -> list[dict]:
    names = {}
    for c in calls:
        if isinstance(c["result"], dict):
            for field, kind in _NAMED.items():
                if isinstance(c["result"].get(field), (int, float)):
                    names.setdefault(round(float(c["result"][field]), 2), kind)
    return [dict(v, kind=names.get(v["value"], "pct" if v["unit"] == "%" else "amount"))
            for v in stated]


def _asks_back(answer: str) -> bool:
    paragraphs = [p for p in (answer or "").strip().split("\n") if p.strip()]
    return bool(paragraphs) and "?" in paragraphs[-1]


def classify(message: str, answer: str, calls: list[dict]) -> dict:
    stated = reply_values(answer, message)
    quotes = _ok_results(calls, "quote_fx")
    reference = None
    if not (answer or "").strip():
        outcome, values = "error", []
    elif any(c["name"] == "escalate_to_human" for c in calls):
        outcome, values = "escalated", _named(calls, stated)
    elif quotes:
        values = _fx_values(quotes[-1]["result"], stated)
        reference = _fx_reference(quotes[-1], values)
        outcome = "fx"
    elif _ok_results(calls, "check_limits"):
        outcome, values = "limits", _named(calls, stated)
    elif not stated and any(isinstance(c["result"], dict) and "error" in c["result"]
                            for c in calls):
        outcome, values = "error", []
    elif not stated and _asks_back(answer):
        outcome, values = "asked", []
    else:
        outcome, values = "answered", _named(calls, stated)
    key = outcome + "|" + "|".join(f"{v['kind']}:{v['value']:.2f}{v['unit']}" for v in values)
    return {"outcome": outcome, "values": values, "reference": reference, "key": key}


def tool_brief(calls: list[dict], limit: int = 160) -> list[str]:
    out = []
    for c in calls:
        text = json.dumps(c["result"], ensure_ascii=False)
        out.append(f"{c['name']} → {text if len(text) <= limit else text[:limit] + '…'}")
    return out


def group(arms: dict[str, list[dict]]) -> list[dict]:
    rows: dict[str, dict] = {}
    for arm, runs in arms.items():
        for i, run in enumerate(runs):
            facts = run["facts"]
            row = rows.get(facts["key"])
            if row is None:
                row = rows[facts["key"]] = {
                    "key": facts["key"], "outcome": facts["outcome"],
                    "values": facts["values"], "reference": facts["reference"],
                    "sample": {"arm": arm, "run": i + 1, "answer": run["answer"]},
                    "arms": {a: {"count": 0, "runs": []} for a in arms}}
            row["arms"][arm]["count"] += 1
            row["arms"][arm]["runs"].append(i + 1)
    return sorted(rows.values(), key=lambda r: -sum(a["count"] for a in r["arms"].values()))
