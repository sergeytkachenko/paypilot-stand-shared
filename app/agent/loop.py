""
import json
import uuid
from contextlib import contextmanager
from contextvars import ContextVar

from app import budget, config, defects, runctx, session_ctx
from app.agent import language, pricing, prompt, router, summarize, tools
from app.agent.providers.base import get_provider
from app.tracing import RequestTrace

_sessions: dict[tuple[str, str], dict] = {}
_owner: ContextVar[str] = ContextVar("session_owner", default="")

_EMPTY_RETRY_LIMIT = 8


def _is_empty_result(result: dict) -> bool:
    if not result:
        return True
    for value in result.values():
        if isinstance(value, (list, dict)) and not value:
            return True
    return False


def _session(session_id: str | None) -> tuple[str, dict]:
    sid = session_id or uuid.uuid4().hex[:12]
    state = _sessions.setdefault((_owner.get(), sid), {"messages": [], "steps": 0})
    return sid, state


@contextmanager
def owned_by(owner: str):
    token = _owner.set(owner)
    try:
        yield
    finally:
        _owner.reset(token)


def reset_sessions(owner: str | None = None) -> None:
    if owner is None:
        _sessions.clear()
        return
    for k in [k for k in _sessions if k[0] == owner]:
        del _sessions[k]


def _messages_for_model(state: dict) -> list[dict]:
    messages = [dict(m) for m in state["messages"]]
    if defects.is_on("D15"):
        earlier_tool_msgs = [m for m in messages if m["role"] == "tool"]
        if earlier_tool_msgs:
            replay = "\n\n".join(
                f"[replayed tool result: {m['name']}]\n{m['content']}"
                for m in earlier_tool_msgs)
            for m in messages:
                if m["role"] == "user":
                    m["content"] = f"(context replay)\n{replay}\n\n{m['content']}"
                    break
    return messages


class SessionCustomerConflict(Exception):
    def __init__(self, bound: str):
        super().__init__(f"session belongs to {bound}; start a new session")
        self.bound = bound


def run_turn(session_id: str | None, user_message: str,
             customer_id: str | None = None) -> dict:
    sid, state = _session(session_id)
    if state["steps"] == 0:
        state["customer_id"] = customer_id
    elif customer_id and customer_id != state.get("customer_id"):
        raise SessionCustomerConflict(state.get("customer_id") or "no customer")
    with session_ctx.bind(state.get("customer_id")):
        return _run_turn(sid, state, user_message)


def _run_turn(sid: str, state: dict, user_message: str) -> dict:
    state["steps"] += 1
    trace = RequestTrace(sid, state["steps"])
    provider = get_provider()
    system, prompt_version = prompt.build()
    system, reply_language = language.apply(system, user_message)
    trace.root.attributes["prompt.version"] = prompt_version
    trace.root.attributes["reply.language_detected"] = reply_language or ""
    trace.root.attributes["llm.provider"] = provider.name
    trace.root.attributes["session.customer_id"] = session_ctx.current() or ""
    trace.root.attributes["context.replay_active"] = (
        defects.is_on("D15") and any(m["role"] == "tool" for m in state["messages"]))
    trace.root.attributes["run.trace_tag"] = _trace_tag()
    trace.root.attributes["run.model"] = getattr(provider, "model", "")
    trace.root.attributes["run.reasoning"] = (
        runctx.current().reasoning or config.LLM_REASONING)
    if config.TRACE_CAPTURE_CONTENT:
        trace.root.attributes["request.message"] = user_message
    provider_costs: list[float | None] = []

    if summarize.should_summarize(state["steps"]) and not state.get("summarized"):
        with trace.span("agent.summarize") as s:
            summary, transcript, summary_resp = summarize.summarize_call(
                provider, state["messages"])
            s.attributes.update({
                "summary.text": summary,
                "summary.replaced_messages": len(state["messages"]),
            })
            s.attributes.update(_usage_attributes(summary_resp))
            if config.TRACE_CAPTURE_CONTENT:
                s.attributes["summary.input"] = transcript
        provider_costs.append(summary_resp.provider_cost_usd)
        state["messages"] = [{"role": "user",
                              "content": f"(summary of earlier conversation)\n{summary}"}]
        state["summarized"] = True

    state["messages"].append({"role": "user", "content": user_message})
    answer = None
    total_in = total_out = cache_read = 0
    model = getattr(provider, "model", "")
    call_costs: list[float | None] = []
    system, router_cost = _route(trace, state, system, user_message)
    if router_cost is not None:
        call_costs.append(router_cost)

    for step in range(config.MAX_AGENT_STEPS):
        with trace.span("llm.call", **{"agent.loop_step": step}) as s:
            resp = _complete(provider, system, state, s)
        provider_costs.append(resp.provider_cost_usd)
        total_in += resp.input_tokens
        total_out += resp.output_tokens
        cache_read += resp.cache_read_input_tokens
        model = resp.model or model
        call_costs.append(_call_cost(resp, model))

        if not resp.tool_calls:
            answer = resp.text or ""
            state["messages"].append({"role": "assistant", "content": answer})
            break

        state["messages"].append({"role": "assistant", "content": resp.text,
                                  "tool_calls": resp.tool_calls})
        for tc in resp.tool_calls:
            result = _execute_tool(trace, tc)
            state["messages"].append({
                "role": "tool", "tool_call_id": tc["id"], "name": tc["name"],
                "content": json.dumps(result, ensure_ascii=False)})
            if defects.is_on("D14") and _is_empty_result(result):
                total_in, total_out, cache_read = _d14_retry_loop(
                    trace, provider, system, state, tc, total_in, total_out,
                    cache_read, provider_costs)
    else:
        answer = "I could not complete this request within the step budget."
        state["messages"].append({"role": "assistant", "content": answer})

    if config.TRACE_CAPTURE_CONTENT:
        trace.root.attributes["response.answer"] = answer
    trace.root.attributes.update({
        "gen_ai.usage.total_input_tokens": total_in,
        "gen_ai.usage.total_output_tokens": total_out,
        "gen_ai.usage.total_cache_read_input_tokens": cache_read,
    })
    tree = trace.finish()
    return {"session_id": sid, "request_id": tree["request_id"],
            "answer": answer, "step_number": state["steps"],
            "prompt_version": prompt_version,
            "elapsed_ms": tree.get("duration_ms"),
            "usage": {"input_tokens": total_in, "output_tokens": total_out,
                      "model": model,
                      "cost_usd": pricing.total_cost_usd(call_costs),
                      "provider_cost_usd": pricing.total_cost_usd(provider_costs)}}


def _trace_tag() -> str:
    if config.TRACE_TAG:
        return config.TRACE_TAG
    key = budget.active()
    return key.label if key else ""


def _complete(provider, system: str, state: dict, span):
    stamped = prompt.stamp(system)
    messages = _messages_for_model(state)
    specs = tools.specs()
    resp = provider.complete(stamped, messages, specs)
    span.attributes.update(_usage_attributes(resp))
    if config.TRACE_CAPTURE_CONTENT:
        span.attributes.update(_content_attributes(stamped, messages, specs, resp))
    return resp


def _content_attributes(system: str, messages: list[dict], specs: list[dict], resp) -> dict:
    from app.agent.providers.openai_provider import _to_openai
    answer = {"role": "assistant", "content": resp.text}
    if resp.tool_calls:
        answer["tool_calls"] = [{"id": tc["id"], "type": "function",
                                 "function": {"name": tc["name"],
                                              "arguments": json.dumps(tc["arguments"],
                                                                      ensure_ascii=False)}}
                                for tc in resp.tool_calls]
    if resp.reasoning_text:
        answer["reasoning"] = resp.reasoning_text
    return {
        "gen_ai.prompt": json.dumps({"messages": _to_openai(system, messages),
                                     "tools": [{"type": "function", "function": {"name": t["name"]}}
                                               for t in specs]},
                                    ensure_ascii=False, default=str),
        "gen_ai.completion": json.dumps(answer, ensure_ascii=False, default=str),
    }


def _usage_attributes(resp) -> dict:
    return {
        "gen_ai.request.model": resp.model,
        "gen_ai.usage.input_tokens": resp.input_tokens,
        "gen_ai.usage.output_tokens": resp.output_tokens,
        "gen_ai.usage.cache_read_input_tokens": resp.cache_read_input_tokens,
        "gen_ai.usage.cache_creation_input_tokens": resp.cache_creation_input_tokens,
        "gen_ai.usage.reasoning_tokens": resp.reasoning_tokens,
        **({"provider.cost_usd": resp.provider_cost_usd}
           if resp.provider_cost_usd is not None else {}),
    }


def _call_cost(resp, model: str) -> float | None:
    return pricing.cost_usd(resp.model or model, resp.input_tokens, resp.output_tokens,
                            cache_read=resp.cache_read_input_tokens,
                            cache_write=resp.cache_creation_input_tokens)


def _route(trace: RequestTrace, state: dict, system: str,
           user_message: str) -> tuple[str, float | None]:
    if state["steps"] != 1 or not router.active_for(defects.current_profile()):
        return system, None
    jev = router.get_router()
    if jev is None:
        return system, None
    with trace.span("router.jev") as s:
        decision = jev.decide(user_message)
        system, block = router.apply(system, decision)
        s.attributes.update({
            "router.model": decision.model,
            "router.intent": decision.intent or "",
            "router.confidence": round(decision.confidence, 3),
            "router.action": decision.action,
            "router.latency_ms": decision.latency_ms,
            "router.added_instructions": block,
            "router.error": decision.error,
        })
    budget.charge(decision.model, decision.input_tokens, 0)
    return system, pricing.cost_usd(decision.model, decision.input_tokens, 0)


def _execute_tool(trace: RequestTrace, tc: dict) -> dict:
    ""
    with trace.span(f"tool.{tc['name']}",
                    **{"tool.name": tc["name"],
                       "tool.arguments": tc["arguments"]}) as s:
        result = tools.dispatch(tc["name"], tc["arguments"])
        s.attributes["tool.result"] = result
        if tc["name"] == "search_knowledge_base" and isinstance(result, dict):
            s.attributes.update({
                "retrieval.query": tc["arguments"].get("query"),
                "retrieval.index": result.get("index"),
                "retrieval.top_k": result.get("top_k"),
                "retrieval.fragments": [
                    {"id": f["id"], "score": f["score"],
                     "text": (f.get("text") or "")[:160]}
                    for f in result.get("fragments", [])],
            })
    return result


def _d14_retry_loop(trace: RequestTrace, provider, system: str, state: dict,
                    tc: dict, total_in: int, total_out: int,
                    cache_read: int,
                    provider_costs: list[float | None]) -> tuple[int, int, int]:
    ""
    for attempt in range(1, _EMPTY_RETRY_LIMIT):
        with trace.span("llm.call", **{"agent.loop_step": f"retry-{attempt}",
                                       "retry.attempt": attempt}) as s:
            probe = _complete(provider, system, state, s)
        provider_costs.append(probe.provider_cost_usd)
        total_in += probe.input_tokens
        total_out += probe.output_tokens
        cache_read += probe.cache_read_input_tokens

        retry_tc = {"id": uuid.uuid4().hex[:12], "name": tc["name"],
                    "arguments": tc["arguments"]}
        state["messages"].append({"role": "assistant", "content": None,
                                  "tool_calls": [retry_tc]})
        result = _execute_tool(trace, retry_tc)
        state["messages"].append({
            "role": "tool", "tool_call_id": retry_tc["id"],
            "name": retry_tc["name"],
            "content": json.dumps(result, ensure_ascii=False)})
        if not _is_empty_result(result):
            break
    return total_in, total_out, cache_read
