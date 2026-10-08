""
import re

PRICES_CHECKED_ON = "2026-10-08"

PRICES_PER_MTOK_USD = {
    "claude-haiku-4-5": (1.0, 5.0),
}

_DATED_SUFFIX = re.compile(r"-\d{8}$")


def price_key(model: str | None) -> str | None:
    if not model:
        return None
    name = model.strip().lower().rsplit("/", 1)[-1]
    name = _DATED_SUFFIX.sub("", name)
    return name if name in PRICES_PER_MTOK_USD else None


def cost_usd(model: str | None, input_tokens: int, output_tokens: int) -> float | None:
    key = price_key(model)
    if key is None:
        return None
    price_in, price_out = PRICES_PER_MTOK_USD[key]
    return round((input_tokens * price_in + output_tokens * price_out) / 1_000_000, 6)


def total_cost_usd(costs: list[float | None]) -> float | None:
    if not costs or any(c is None for c in costs):
        return None
    return round(sum(costs), 6)
