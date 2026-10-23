""
import base64
import json
import os

from app import config

_enabled = False
_tracer = None
_backends: list[str] = []

OBSERVATION_TYPES = {
    "agent.request": "agent",
    "llm.call": "generation",
    "agent.summarize": "generation",
    "router.jev": "generation",
    "tool.search_knowledge_base": "retriever",
}


def _parse_headers(raw: str) -> dict:
    headers = {}
    for part in raw.split(","):
        name, sep, value = part.partition("=")
        if sep and name.strip():
            headers[name.strip()] = value.strip()
    return headers


def langfuse_target() -> tuple[str, dict] | None:
    if not (config.LANGFUSE_HOST and config.LANGFUSE_PUBLIC_KEY and config.LANGFUSE_SECRET_KEY):
        return None
    token = base64.b64encode(
        f"{config.LANGFUSE_PUBLIC_KEY}:{config.LANGFUSE_SECRET_KEY}".encode()).decode()
    return (config.LANGFUSE_HOST + "/api/public/otel/v1/traces",
            {"Authorization": f"Basic {token}", "x-langfuse-ingestion-version": "4"})


def targets() -> list[tuple[str, str, dict]]:
    found = []
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if endpoint:
        found.append(("otlp", endpoint.rstrip("/") + "/v1/traces",
                      _parse_headers(os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", ""))))
    langfuse = langfuse_target()
    if langfuse:
        found.append(("langfuse", *langfuse))
    return found


def init() -> bool:
    ""
    global _enabled, _tracer, _backends
    found = targets()
    if not found:
        return False
    try:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter)
        provider = TracerProvider(resource=Resource.create(
            {"service.name": os.environ.get("OTEL_SERVICE_NAME", "paypilot-stand")}))
        for _, url, headers in found:
            provider.add_span_processor(BatchSpanProcessor(
                OTLPSpanExporter(endpoint=url, headers=headers or None)))
        _tracer = provider.get_tracer("paypilot.stand")
        _backends = [name for name, _, _ in found]
        _enabled = True
    except Exception:
        _enabled = False
        _backends = []
    return _enabled


def backends() -> list[str]:
    return list(_backends)


def _flatten(value):
    if isinstance(value, (str, bool, int, float)) or value is None:
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def observation_type(name: str) -> str:
    if name in OBSERVATION_TYPES:
        return OBSERVATION_TYPES[name]
    if name.startswith("tool."):
        return "tool"
    return "span"


def _model(tree: dict) -> str:
    for child in tree.get("children", []):
        model = (child.get("attributes") or {}).get("gen_ai.request.model")
        if child["name"] == "llm.call" and model:
            return model
    return ""


def trace_attributes(tree: dict) -> dict:
    root = tree.get("attributes") or {}
    profile = root.get("run.profile") or ""
    tags = [profile, *(root.get("run.active_defects") or []), _model(tree),
            root.get("run.trace_tag") or ""]
    out = {
        "session.id": root.get("session.id") or "",
        "user.id": root.get("session.customer_id") or "",
        "langfuse.environment": profile or "default",
        "langfuse.release": config.STAND_RELEASE,
        "langfuse.trace.tags": [t for t in dict.fromkeys(tags) if t],
    }
    return {k: v for k, v in out.items() if v not in ("", [])}


def usage_details(attrs: dict) -> dict:
    total_in = attrs.get("gen_ai.usage.input_tokens") or 0
    cached = attrs.get("gen_ai.usage.cache_read_input_tokens") or 0
    written = attrs.get("gen_ai.usage.cache_creation_input_tokens") or 0
    total_out = attrs.get("gen_ai.usage.output_tokens") or 0
    reasoning = min(attrs.get("gen_ai.usage.reasoning_tokens") or 0, total_out)
    return {"input": max(total_in - cached - written, 0),
            "input_cached_tokens": cached,
            "input_cache_creation_tokens": written,
            "output": total_out - reasoning,
            "output_reasoning_tokens": reasoning}


def _prompt_reference(version: str) -> dict:
    base, _, overlays = version.partition("+")
    name, _, number = base.partition(".v")
    out = {"langfuse.observation.metadata.prompt_version": version}
    if name and number.isdigit():
        out["langfuse.observation.prompt.name"] = name
        out["langfuse.observation.prompt.version"] = int(number)
    return out


def langfuse_attributes(node: dict, tree: dict) -> dict:
    from app.agent import pricing
    attrs = node.get("attributes") or {}
    root = tree.get("attributes") or {}
    out = {"langfuse.observation.type": observation_type(node["name"]),
           **trace_attributes(tree)}
    if node is tree:
        out["langfuse.trace.metadata.step"] = root.get("dialog.step_number")
        if "request.message" in root:
            out["langfuse.observation.input"] = root["request.message"]
        if "response.answer" in root:
            out["langfuse.observation.output"] = root["response.answer"]
    if "gen_ai.usage.input_tokens" in attrs:
        out["langfuse.observation.usage_details"] = json.dumps(usage_details(attrs))
        cost = pricing.cost_usd(attrs.get("gen_ai.request.model"),
                                attrs.get("gen_ai.usage.input_tokens") or 0,
                                attrs.get("gen_ai.usage.output_tokens") or 0,
                                cache_read=attrs.get("gen_ai.usage.cache_read_input_tokens") or 0,
                                cache_write=attrs.get("gen_ai.usage.cache_creation_input_tokens") or 0)
        if cost is not None:
            out["stand.cost_usd"] = cost
    if node["name"] == "llm.call" and root.get("prompt.version"):
        out.update(_prompt_reference(root["prompt.version"]))
    if node["name"] == "agent.summarize":
        if "summary.input" in attrs:
            out["langfuse.observation.input"] = attrs["summary.input"]
        if "summary.text" in attrs:
            out["langfuse.observation.output"] = attrs["summary.text"]
    return {k: v for k, v in out.items() if v is not None}


def export_tree(tree: dict) -> None:
    ""
    if not _enabled or _tracer is None:
        return
    try:
        _emit(tree, tree, parent_ctx=None)
    except Exception:
        pass


def exported_attributes(attrs: dict) -> dict:
    out = {}
    for k, v in attrs.items():
        if k.startswith("gen_ai.usage.total_"):
            k = "stand.usage." + k[len("gen_ai.usage."):]
        flat = _flatten(v)
        if flat is not None:
            out[k] = flat
    return out


def _nanos(seconds) -> int | None:
    return int(seconds * 1e9) if seconds else None


def _emit(node: dict, tree: dict, parent_ctx):
    from opentelemetry import trace
    ctx = trace.set_span_in_context(parent_ctx) if parent_ctx else None
    span = _tracer.start_span(node["name"], context=ctx,
                              start_time=_nanos(node.get("started_at")))
    try:
        for k, v in exported_attributes(node.get("attributes") or {}).items():
            span.set_attribute(k, v)
        for k, v in langfuse_attributes(node, tree).items():
            span.set_attribute(k, v)
        for child in node.get("children", []):
            _emit(child, tree, span)
    finally:
        span.end(end_time=_nanos(node.get("ended_at") or node.get("started_at")))
