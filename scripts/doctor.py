"""Environment diagnostics (ТЗ §5.9): checks dependencies, keys, versions,
model access — and says in plain words what is missing. Exit code 0 = green."""
import importlib
import os
import sys

# Windows consoles default to cp1252: any non-ASCII in the output kills the run
# with UnicodeEncodeError before the verdict is printed. Force UTF-8.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# Read .env exactly the way the app does, so doctor reports what the stand
# will actually see. Before this, doctor said "provider: mock, clock not set"
# straight after the key had been filled in correctly — the single most
# misleading line in the first-run instructions.
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from app import config  # noqa: E402,F401  (import populates os.environ from .env)

OK, WARN, FAIL = "OK", "WARN", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, status: str, detail: str = ""):
    results.append((name, status, detail))


def main() -> int:
    if sys.version_info >= (3, 10):
        check("python", OK, sys.version.split()[0])
    else:
        check("python", FAIL, f"{sys.version.split()[0]} — need 3.10+")

    for mod in ("fastapi", "uvicorn", "pydantic", "yaml", "httpx", "pytest"):
        try:
            importlib.import_module(mod)
            check(f"dependency: {mod}", OK)
        except ImportError:
            check(f"dependency: {mod}", FAIL,
                  "run: pip install -r requirements.txt")

    provider = os.environ.get("LLM_PROVIDER", "mock")
    if provider == "mock":
        check("llm provider", WARN,
              "mock — free and deterministic, but probabilistic defects need "
              "a live provider; set LLM_PROVIDER=anthropic|openai")
    elif provider == "anthropic":
        key = os.environ.get("ANTHROPIC_API_KEY", "")
        check("llm provider", OK if key else FAIL,
              "anthropic" if key else "ANTHROPIC_API_KEY is empty")
        if key:
            _probe_anthropic(key)
    elif provider == "openai":
        key = os.environ.get("OPENAI_API_KEY", "")
        check("llm provider", OK if key else FAIL,
              "openai" if key else "OPENAI_API_KEY is empty")
        if key:
            _probe_openai(key)
    else:
        check("llm provider", FAIL,
              f"unknown LLM_PROVIDER={provider!r} (mock | anthropic | openai)")

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    if endpoint:
        try:
            import httpx
            r = httpx.get(endpoint.rstrip("/"), timeout=5)
            check("phoenix (trace UI)", OK if r.status_code < 500 else WARN,
                  endpoint)
        except Exception:
            check("phoenix (trace UI)", WARN,
                  f"{endpoint} unreachable — traces still land in traces/traces.jsonl "
                  "and /api/_test/traces")
    else:
        check("phoenix (trace UI)", WARN,
              "OTEL_EXPORTER_OTLP_ENDPOINT not set; the JSONL/API trace surface "
              "still works")

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for artefact in ("prompts/base.v1.md", "specs/requirements/US-01.md"):
        path = os.path.join(root, artefact)
        check(f"artefact: {artefact}", OK if os.path.exists(path) else FAIL,
              "" if os.path.exists(path) else "missing — L01 lab needs it")

    clock = os.environ.get("CLOCK_OVERRIDE", "")
    check("clock override", OK if clock else WARN,
          clock or "not set — window-sensitive cases will drift with real time")

    profile = os.environ.get("PROFILE", "clean")
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from app import defects
        defects.validate_startup()
        check("profile/defects config", OK,
              f"PROFILE={profile} DEFECTS={os.environ.get('DEFECTS', '') or '-'}")
    except Exception as e:
        check("profile/defects config", FAIL, str(e))

    from app import budget
    if not config.STAND_KEYS_REQUIRED:
        check("student keys", OK, "off — spending routes are open (STAND_KEYS_REQUIRED=0)")
    elif budget.unpriced_models():
        check("student keys", FAIL,
              "no price in app/agent/pricing.py for " + ", ".join(budget.unpriced_models()))
    else:
        check("student keys", OK,
              f"on — {len(budget.all_keys())} keys, defaults "
              f"${config.STAND_KEY_DAILY_USD:g}/24h, ${config.STAND_KEY_MONTHLY_USD:g}/30d")

    width = max(len(n) for n, _, _ in results) + 2
    worst = OK
    for name, status, detail in results:
        print(f"{name:<{width}} [{status}] {detail}")
        if status == FAIL:
            worst = FAIL
        elif status == WARN and worst != FAIL:
            worst = WARN
    print()
    print({"OK": "Environment is ready.",
           "WARN": "Usable with warnings — read the lines above.",
           "FAIL": "Environment is NOT ready — fix the [FAIL] lines above."}[worst])
    return 1 if worst == FAIL else 0


def _probe_anthropic(key: str):
    try:
        import time

        import httpx

        from app.agent.providers.anthropic_provider import DEFAULT_MODEL
        # A balance top-up can take a moment to propagate, so a single 400 is
        # not proof the key is unusable: retry briefly before reporting.
        for attempt in range(3):
            r = httpx.post("https://api.anthropic.com/v1/messages",
                           headers={"x-api-key": key,
                                    "anthropic-version": "2023-06-01"},
                           json={"model": config.LLM_MODEL or DEFAULT_MODEL, "max_tokens": 1,
                                 "messages": [{"role": "user",
                                               "content": "ping"}]},
                           timeout=15)
            if r.status_code == 200 or attempt == 2:
                break
            time.sleep(3)
        if r.status_code == 200:
            check("anthropic api reachable", OK, "HTTP 200")
        else:
            hint = ""
            body = r.text[:300]
            if "credit balance is too low" in body:
                hint = (" — the organisation is out of credits; top up at "
                        "console.anthropic.com -> Plans & Billing")
            elif r.status_code == 401:
                hint = " — the key is invalid or revoked"
            elif r.status_code == 429:
                hint = " — rate limited; retry shortly"
            check("anthropic api reachable", FAIL,
                  f"HTTP {r.status_code}{hint}")
    except Exception as e:
        check("anthropic api reachable", FAIL, str(e))


def _probe_openai(key: str):
    try:
        import httpx
        r = httpx.get("https://api.openai.com/v1/models",
                      headers={"Authorization": f"Bearer {key}"}, timeout=15)
        check("openai api reachable", OK if r.status_code == 200 else FAIL,
              f"HTTP {r.status_code}")
    except Exception as e:
        check("openai api reachable", FAIL, str(e))


if __name__ == "__main__":
    sys.exit(main())
