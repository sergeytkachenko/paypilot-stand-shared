import yaml

from app import config

with open(config.MODELS_FILE, encoding="utf-8") as f:
    WHITELIST: dict[str, dict] = yaml.safe_load(f)["models"]

REASONING_LEVELS = ("off", "low", "medium", "high")


def known() -> list[str]:
    return sorted(WHITELIST)


def uses_cache_control(model: str) -> bool:
    return bool(WHITELIST.get(model, {}).get("cache_control", True))


def reasoning_payload(level: str | None) -> dict:
    if not level:
        return {}
    if level == "off":
        return {"reasoning": {"enabled": False}}
    return {"reasoning": {"effort": level}}
