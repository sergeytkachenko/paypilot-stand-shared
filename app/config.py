""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    ""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()
PROMPTS_DIR = ROOT / "prompts"
PROFILES_FILE = ROOT / "profiles" / "profiles.yaml"
DEFECTS_FILE = ROOT / "profiles" / "defects.yaml"
CORPUS_DIR = ROOT / "app" / "rag" / "corpus"
DATA_DIR = ROOT / "data"
TRACES_DIR = ROOT / "traces"

PROFILE = os.environ.get("PROFILE", "clean")
DEFECTS_ENV = os.environ.get("DEFECTS", "")

LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "mock")
LLM_MODEL = os.environ.get("LLM_MODEL", "")


def _optional_float(name: str) -> float | None:
    raw = os.environ.get(name, "").strip()
    if raw == "":
        return None
    try:
        return float(raw)
    except ValueError:
        raise RuntimeError(f"{name} must be a number, got {raw!r}")


LLM_TEMPERATURE = _optional_float("LLM_TEMPERATURE")
EXPLAIN_MODEL = os.environ.get("EXPLAIN_MODEL", "")
ROUTER = os.environ.get("ROUTER", "").strip().lower()
ROUTER_PROFILES = {p.strip() for p in os.environ.get("ROUTER_PROFILES", "clean").split(",")
                   if p.strip()}
_router_min_confidence = _optional_float("ROUTER_MIN_CONFIDENCE")
ROUTER_MIN_CONFIDENCE = 0.9 if _router_min_confidence is None else _router_min_confidence
TYPESAFE_API_KEY = os.environ.get("TYPESAFE_API_KEY", "")
TYPESAFE_MODEL = os.environ.get("TYPESAFE_MODEL", "jev-latest")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

MAX_AGENT_STEPS = int(os.environ.get("MAX_AGENT_STEPS", "12"))
SUMMARIZE_AFTER_STEPS = int(os.environ.get("SUMMARIZE_AFTER_STEPS", "8"))

KB_INDEX_ENV = os.environ.get("KB_INDEX", "")
RAG_TOP_K = int(os.environ.get("RAG_TOP_K", "4"))

CLOCK_OVERRIDE = os.environ.get("CLOCK_OVERRIDE", "")

STAND_LOCK_GLOBAL = os.environ.get("STAND_LOCK_GLOBAL", "").lower() in ("1", "true", "yes")
STAND_ADMIN_TOKEN = os.environ.get("STAND_ADMIN_TOKEN", "")
STAND_KEYS_REQUIRED = os.environ.get("STAND_KEYS_REQUIRED", "").lower() in ("1", "true", "yes")
_key_daily_usd = _optional_float("STAND_KEY_DAILY_USD")
STAND_KEY_DAILY_USD = 1.0 if _key_daily_usd is None else _key_daily_usd
_key_monthly_usd = _optional_float("STAND_KEY_MONTHLY_USD")
STAND_KEY_MONTHLY_USD = 5.0 if _key_monthly_usd is None else _key_monthly_usd

DATA_DIR.mkdir(exist_ok=True)
TRACES_DIR.mkdir(exist_ok=True)
