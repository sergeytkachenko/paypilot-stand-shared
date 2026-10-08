""
import hmac
import os
import threading
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from app import answers, clock, config, db, defects, otel, runctx, tracing
from app.agent import explain, loop, pricing, prompt, router, summarize, tools
from app.engines import reference

defects.validate_startup()
db.ensure_seeded()
otel.init()

app = FastAPI(title="PayPilot stand", version="0.1.0")

STATIC_DIR = config.ROOT / "app" / "static"

LOCKED_DETAIL = (
    "Це спільний стенд: серверні PUT і скидання бази закриті. Профіль, дефекти, "
    f"top_k, індекс, годинник і згортку задавайте заголовком {runctx.HEADER} "
    "(панель чату робить це сама); чат, clean vs профіль і ×5 працюють без нього. "
    "Скрипти з PUT, eval-прогони і скидання бази — на власному стенді. "
    f"Лектор відкриває серверні дії заголовком {runctx.ADMIN_HEADER}.")


@app.middleware("http")
async def request_settings(request: Request, call_next):
    raw = request.headers.get(runctx.HEADER)
    try:
        settings = runctx.parse_header(raw) if raw else runctx.RunSettings()
    except ValueError as e:
        return JSONResponse({"detail": str(e)}, status_code=400)
    token = runctx.activate(settings)
    try:
        return await call_next(request)
    finally:
        runctx.deactivate(token)


def is_admin(request: Request) -> bool:
    supplied = request.headers.get(runctx.ADMIN_HEADER, "")
    return bool(config.STAND_ADMIN_TOKEN) and hmac.compare_digest(
        supplied.encode(), config.STAND_ADMIN_TOKEN.encode())


def require_server_write(request: Request) -> None:
    if config.STAND_LOCK_GLOBAL and not is_admin(request):
        raise HTTPException(403, LOCKED_DETAIL)


@app.get("/", include_in_schema=False)
def chat_ui():
    ""
    return FileResponse(STATIC_DIR / "index.html")


class ChatIn(BaseModel):
    message: str
    session_id: str | None = None
    customer_id: str | None = None


def _signed_in(customer_id: str | None) -> str | None:
    if not customer_id:
        return None
    cid = customer_id.strip().upper()
    if not db.one("SELECT id FROM customers WHERE id = ?", (cid,)):
        raise HTTPException(400, f"Unknown customer {customer_id!r}")
    return cid


@app.post("/chat")
def chat(body: ChatIn):
    customer = _signed_in(body.customer_id)
    try:
        return loop.run_turn(body.session_id, body.message, customer)
    except loop.SessionCustomerConflict as e:
        raise HTTPException(409, str(e))


class CompareIn(BaseModel):
    message: str
    profile: str | None = None
    customer_id: str | None = None


def _arm_conditions() -> dict:
    from app.rag import retriever
    return {
        "retrieval": {"index": retriever.active_index_name(),
                      "top_k": retriever.active_top_k()},
        "clock": clock.describe()["now"],
        "router": router.describe() if router.active_for(defects.current_profile()) else "",
    }


def _model_of(request_id: str) -> str | None:
    tree = tracing.get(request_id) or {}
    first_call = next((c for c in tree.get("children", [])
                       if c.get("name") == "llm.call"), None)
    if first_call is None:
        return None
    return first_call.get("attributes", {}).get("gen_ai.request.model")


def _tools_called(request_id: str) -> list[str]:
    tree = tracing.get(request_id) or {}
    return [c["name"][len("tool."):] for c in tree.get("children", [])
            if c.get("name", "").startswith("tool.")]


def _fresh_run(message: str, customer_id: str | None) -> dict:
    conditions = _arm_conditions()
    res = loop.run_turn(None, message, customer_id)
    calls = answers.tool_calls(res["request_id"])
    return {
        "profile": defects.current_profile(),
        "active_defects": sorted(defects.active()),
        "answer": res["answer"],
        "request_id": res["request_id"],
        "usage": res["usage"],
        "prompt_version": res["prompt_version"],
        "step_number": res["step_number"],
        "elapsed_ms": res["elapsed_ms"],
        "model": _model_of(res["request_id"]),
        "tools_called": _tools_called(res["request_id"]),
        "facts": answers.classify(message, res["answer"], calls),
        "tool_brief": answers.tool_brief(calls),
        **conditions,
    }


@app.post("/api/_test/compare")
def test_compare(body: CompareIn):
    ""
    customer = _signed_in(body.customer_id)
    target = body.profile or defects.current_profile()
    if target not in defects.PROFILES:
        raise HTTPException(400, f"Unknown profile {target!r}. "
                                 f"Known: {sorted(defects.PROFILES)}")
    extra = ",".join(defects.describe()["extra_defects"])
    out = {"scope": "request"}
    for label, prof, arm_extra in (("clean", "clean", ""),
                                   ("profile", target, extra)):
        with runctx.override(profile=prof, defects=arm_extra):
            out[label] = _fresh_run(body.message, customer)
    return out


class SeriesIn(BaseModel):
    message: str
    runs: int = 5
    profile: str | None = None
    customer_id: str | None = None


SERIES_MAX_RUNS = 5
_series_slots = threading.BoundedSemaphore(2)


def _series_arm(message: str, runs: int, customer_id: str | None) -> dict:
    results = [_fresh_run(message, customer_id) for _ in range(runs)]
    counts: dict[str, int] = {}
    for r in results:
        for name in set(r["tools_called"]):
            counts[name] = counts.get(name, 0) + 1
    return {
        "profile": results[0]["profile"],
        "active_defects": results[0]["active_defects"],
        "prompt_version": results[0]["prompt_version"],
        "runs": results,
        "tool_counts": counts,
        "usage": {
            "input_tokens": sum(r["usage"]["input_tokens"] for r in results),
            "output_tokens": sum(r["usage"]["output_tokens"] for r in results),
            "cost_usd": pricing.total_cost_usd(
                [r["usage"].get("cost_usd") for r in results])},
        "elapsed_ms": sum(r["elapsed_ms"] or 0 for r in results),
    }


@app.post("/api/_test/series")
def test_series(body: SeriesIn):
    ""
    runs = max(1, min(body.runs, SERIES_MAX_RUNS))
    customer = _signed_in(body.customer_id)
    if body.profile is not None and body.profile not in defects.PROFILES:
        raise HTTPException(400, f"Unknown profile {body.profile!r}. "
                                 f"Known: {sorted(defects.PROFILES)}")
    if not _series_slots.acquire(blocking=False):
        raise HTTPException(503, "Стенд уже виконує дві серії; зачекайте, поки "
                                 "вони завершаться, і натисніть ще раз")
    try:
        out = {"runs": runs, "scope": "request", "arms": {}}
        out["arms"]["current"] = _series_arm(body.message, runs, customer)
        if body.profile is not None:
            with runctx.override(profile=body.profile, defects=""):
                out["arms"]["profile"] = _series_arm(body.message, runs, customer)
        order = ["profile", "current"] if "profile" in out["arms"] else ["current"]
        out["rows"] = answers.group({a: out["arms"][a]["runs"] for a in order})
        return out
    finally:
        _series_slots.release()


class ExplainArm(BaseModel):
    request_id: str
    answer: str = ""


class ExplainIn(BaseModel):
    message: str
    lang: Literal["uk", "en"] = "uk"
    clean: ExplainArm
    profile: ExplainArm


@app.post("/api/_test/compare/explain")
def test_compare_explain(body: ExplainIn):
    trees = {}
    for label, arm in (("clean", body.clean), ("profile", body.profile)):
        tree = tracing.get(arm.request_id)
        if tree is None:
            raise HTTPException(404, f"trace {arm.request_id} not found ({label})")
        trees[label] = tree
    facts = explain.build_facts(body.message, trees["clean"], trees["profile"],
                                body.clean.answer, body.profile.answer)
    try:
        return explain.explain(facts, body.lang)
    except Exception as e:
        raise HTTPException(502, f"explain model failed: {e}")


@app.get("/health")
def health(request: Request):
    return {"status": "ok", "profile": defects.current_profile(),
            "startup_profile": config.PROFILE,
            "active_defects": sorted(defects.active()),
            "provider": config.LLM_PROVIDER,
            "router": router.describe(),
            "otlp": bool(os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")),
            "scope": runctx.scope(),
            "request_settings": runctx.current().as_dict(),
            "locked": config.STAND_LOCK_GLOBAL,
            "admin": is_admin(request)}



@app.get("/api/_test/defects")
def test_defects():
    return defects.describe()


class ProfileIn(BaseModel):
    profile: str | None = None


@app.put("/api/_test/profile", dependencies=[Depends(require_server_write)])
def test_set_profile(body: ProfileIn):
    try:
        defects.set_runtime_profile(body.profile)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return defects.describe()


class DefectsIn(BaseModel):
    defects: str | None = None


@app.put("/api/_test/defects", dependencies=[Depends(require_server_write)])
def test_set_defects(body: DefectsIn):
    try:
        defects.set_runtime_defects(body.defects)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return defects.describe()


@app.get("/api/_test/state/{table}")
def test_state(table: str):
    try:
        return {"table": table, "rows": db.table_dump(table)}
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.get("/api/_test/seed")
def test_seed():
    from app import seed
    return {"seed_version": seed.SEED_VERSION,
            "customers": db.table_dump("customers"),
            "accounts": db.table_dump("accounts"),
            "transaction_count": len(db.table_dump("transactions"))}


@app.get("/api/_test/reference")
def test_reference():
    return reference.tables()


@app.get("/api/_test/clock")
def test_clock():
    return clock.describe()


class ClockIn(BaseModel):
    now: str | None = None


@app.post("/api/_test/clock", dependencies=[Depends(require_server_write)])
@app.put("/api/_test/clock", dependencies=[Depends(require_server_write)])
def test_set_clock(body: ClockIn):
    try:
        clock.set_override(body.now)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return clock.describe()


@app.get("/api/_test/retrieval")
def test_retrieval():
    from app.rag import retriever
    return {"index": retriever.active_index_name(),
            "top_k": retriever.active_top_k(),
            "requested_index": retriever.requested_index(),
            "requested_top_k": retriever.requested_top_k(),
            "index_env": config.KB_INDEX_ENV, "rag_top_k": config.RAG_TOP_K,
            "scope": runctx.scope()}


class RetrievalIn(BaseModel):
    top_k: int | None = None
    index: str | None = None


@app.put("/api/_test/retrieval", dependencies=[Depends(require_server_write)])
def test_set_retrieval(body: RetrievalIn):
    ""
    from app.rag import retriever
    if body.top_k is not None:
        if not 1 <= body.top_k <= 20:
            raise HTTPException(400, "top_k must be between 1 and 20")
        config.RAG_TOP_K = body.top_k
    if body.index is not None:
        if body.index not in ("kb_clean", "kb_broken", ""):
            raise HTTPException(400, "index must be kb_clean, kb_broken or \"\"")
        config.KB_INDEX_ENV = body.index
    return {"index": retriever.active_index_name(),
            "top_k": retriever.active_top_k(),
            "requested_index": retriever.requested_index(),
            "requested_top_k": retriever.requested_top_k(),
            "index_env": config.KB_INDEX_ENV, "rag_top_k": config.RAG_TOP_K,
            "scope": runctx.scope()}


@app.get("/api/_test/summarize_after")
def test_summarize_after():
    return {"summarize_after_steps": summarize.summarize_after_steps(),
            "scope": runctx.scope()}


class SummarizeIn(BaseModel):
    steps: int


@app.put("/api/_test/summarize_after", dependencies=[Depends(require_server_write)])
def test_set_summarize_after(body: SummarizeIn):
    ""
    if body.steps < 1:
        raise HTTPException(400, "steps must be >= 1")
    config.SUMMARIZE_AFTER_STEPS = body.steps
    return {"summarize_after_steps": summarize.summarize_after_steps(),
            "scope": runctx.scope()}


@app.post("/api/_test/reset", dependencies=[Depends(require_server_write)])
def test_reset():
    info = db.reset()
    loop.reset_sessions()
    return {"status": "reset", **info}


@app.get("/api/_test/prompt")
def test_prompt():
    text, version = prompt.build()
    return {"version": version, "overlays": prompt.active_overlays(),
            "text": text}


@app.get("/api/_test/specs")
def test_specs():
    ""
    out = {}
    for path in sorted((config.ROOT / "specs" / "requirements").glob("*.md")):
        out[path.name] = path.read_text(encoding="utf-8")
    return {"requirements": out}


@app.get("/api/_test/tools")
def test_tools():
    return {"tools": tools.specs()}


@app.get("/api/_test/traces")
def test_traces(limit: int = 20):
    return {"traces": tracing.recent(limit)}


@app.get("/api/_test/traces/{request_id}")
def test_trace(request_id: str):
    tree = tracing.get(request_id)
    if not tree:
        raise HTTPException(404, f"no trace for request {request_id}")
    return tree
