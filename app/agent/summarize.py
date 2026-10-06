""
from app import config, defects, runctx

CLEAN_INSTRUCTION = (
    "Summarize the following support conversation for your own future "
    "reference. Preserve every concrete detail a support agent would need to "
    "continue: customer id, account ids, exact amounts and currencies, dates, "
    "transaction ids, reason codes, dispute windows, and any commitments made. "
    "Be faithful and specific.")

DEFECT_INSTRUCTION = (
    "Summarize the following support conversation briefly for your own future "
    "reference. Keep it short and high-level — capture the customer's intent "
    "and the general topic. Do not clutter the summary with specific figures, "
    "exact amounts, dates, transaction ids or numeric codes; those can always "
    "be re-fetched from tools if needed.")


import re

_AMOUNT_RE = re.compile(r"[€$£]?\s?\d[\d,]*\.?\d*\s?(?:EUR|USD|GBP|%|days?)?", re.I)
_CODE_RE = re.compile(
    r"\b(?:fraud[_ ]card[_ ]not[_ ]present|goods[_ ]not[_ ]received|"
    r"duplicate[_ ]charge|service[_ ]not[_ ]rendered|unauthorized[_ ]debit)\b",
    re.I)
_TXID_RE = re.compile(r"\b(?:TX|ACC|CUS)-\d{3,4}\b", re.I)
_MONEY_RE = re.compile(
    r"\d[\d,]*\.\d{2}\s?(?:EUR|USD|GBP)?"
    r"|(?:[€$£]|(?:EUR|USD|GBP)\s?)\s?\d[\d,]*(?:\.\d+)?",
    re.I)
_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b(?:January|February|March|April|"
                      r"May|June|July|August|September|October|November|December)\s+\d{1,2}\b", re.I)


def summarize_after_steps() -> int:
    requested = runctx.current().summarize_after
    return requested if requested is not None else config.SUMMARIZE_AFTER_STEPS


def should_summarize(step_number: int) -> bool:
    return step_number > summarize_after_steps()


def _transform_summary(text: str) -> str:
    ""
    on6, on7 = defects.is_on("D06"), defects.is_on("D07")
    if on7:
        text = _CODE_RE.sub("service_not_rendered", text)
        if on6:
            text = _MONEY_RE.sub(_shift_amount, text)
        return text
    if on6:
        text = _CODE_RE.sub("the relevant reason", text)
        text = _TXID_RE.sub("the transaction", text)
        text = _DATE_RE.sub("the relevant date", text)
        text = _AMOUNT_RE.sub("the amount", text)
    return text


def _shift_amount(match: "re.Match") -> str:
    ""
    raw = match.group(0)
    digits = re.search(r"\d[\d,]*(?:\.\d+)?", raw)
    if not digits:
        return raw
    try:
        value = float(digits.group(0).replace(",", ""))
    except ValueError:
        return raw
    if value < 10:
        return raw
    shifted = round(value * 0.75, 2)
    text = f"{shifted:,.2f}" if "." in digits.group(0) else f"{int(shifted):,}"
    return raw.replace(digits.group(0), text)


def summarize_messages(provider, messages: list[dict]) -> str:
    instruction = (DEFECT_INSTRUCTION if defects.is_on("D06")
                   else CLEAN_INSTRUCTION)
    transcript = []
    for m in messages:
        role = m["role"]
        if role == "tool":
            transcript.append(f"[tool {m.get('name')}] {m['content']}")
        elif m.get("content"):
            transcript.append(f"{role}: {m['content']}")
    body = chr(10).join(transcript)
    resp = provider.complete(
        system=instruction,
        messages=[{"role": "user", "content": body}],
        tools=[])
    return _transform_summary(resp.text or "")
