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
LLM_TEMPERATURE = os.environ.get("LLM_TEMPERATURE", "")
EXPLAIN_MODEL = os.environ.get("EXPLAIN_MODEL", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

MAX_AGENT_STEPS = int(os.environ.get("MAX_AGENT_STEPS", "12"))
SUMMARIZE_AFTER_STEPS = int(os.environ.get("SUMMARIZE_AFTER_STEPS", "8"))

KB_INDEX_ENV = os.environ.get("KB_INDEX", "")
RAG_TOP_K = int(os.environ.get("RAG_TOP_K", "4"))

CLOCK_OVERRIDE = os.environ.get("CLOCK_OVERRIDE", "")

STAND_LOCK_GLOBAL = os.environ.get("STAND_LOCK_GLOBAL", "").lower() in ("1", "true", "yes")
STAND_ADMIN_TOKEN = os.environ.get("STAND_ADMIN_TOKEN", "")

DATA_DIR.mkdir(exist_ok=True)
TRACES_DIR.mkdir(exist_ok=True)
