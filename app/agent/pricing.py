""
import re

PRICES_CHECKED_ON = "2026-10-23"

PRICES_PER_MTOK_USD = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-haiku-5-5": (0.10, 0.50),
    "gemini-2.5-flash": (0.30, 2.50),
    "jev-latest": (0.042, 0.0),
    "jev-1.13.0": (0.042, 0.0),
}

LONG_PROMPT_PRICES_PER_MTOK_USD = {
    "claude-haiku-5-5": (100_000, 0.50, 2.50),
}

CACHE_PRICE_MULTIPLIERS = {
    "claude-haiku-4-5": (0.1, 1.25),
    "claude-haiku-5-5": (0.1, 1.25),
    "gemini-2.5-flash": (0.1, 0.3778),
}

CACHE_WRITE_INSIDE_READ = {"gemini-2.5-flash"}

_DATED_SUFFIX = re.compile(r"-\d{8}$")


def price_key(model: str | None) -> str | None:
    if not model:
        return None
    name = model.strip().lower().rsplit("/", 1)[-1]
    name = _DATED_SUFFIX.sub("", name)
    for candidate in (name, name.replace(".", "-")):
        if candidate in PRICES_PER_MTOK_USD:
            return candidate
    return None


def cache_buckets(model: str | None, input_tokens: int, cache_read: int = 0,
                  cache_write: int = 0) -> tuple[int, int, int]:
    if price_key(model) in CACHE_WRITE_INSIDE_READ:
        cache_read = max(cache_read - cache_write, 0)
    uncached = max(input_tokens - cache_read - cache_write, 0)
    return uncached, cache_read, cache_write


def cost_usd(model: str | None, input_tokens: int, output_tokens: int,
             cache_read: int = 0, cache_write: int = 0) -> float | None:
    key = price_key(model)
    if key is None:
        return None
    price_in, price_out = PRICES_PER_MTOK_USD[key]
    long_tier = LONG_PROMPT_PRICES_PER_MTOK_USD.get(key)
    if long_tier and input_tokens > long_tier[0]:
        price_in, price_out = long_tier[1], long_tier[2]
    read_mult, write_mult = CACHE_PRICE_MULTIPLIERS.get(key, (1.0, 1.0))
    uncached, cache_read, cache_write = cache_buckets(
        model, input_tokens, cache_read, cache_write)
    input_cost = (uncached + cache_read * read_mult + cache_write * write_mult) * price_in
    return round((input_cost + output_tokens * price_out) / 1_000_000, 6)


def ceiling_cost_usd(input_tokens: int, output_tokens: int) -> float:
    rates = list(PRICES_PER_MTOK_USD.values()) + [
        (tier[1], tier[2]) for tier in LONG_PROMPT_PRICES_PER_MTOK_USD.values()]
    price_in = max(r[0] for r in rates)
    price_out = max(r[1] for r in rates)
    return round((input_tokens * price_in + output_tokens * price_out) / 1_000_000, 6)


def total_cost_usd(costs: list[float | None]) -> float | None:
    if not costs or any(c is None for c in costs):
        return None
    return round(sum(costs), 6)
