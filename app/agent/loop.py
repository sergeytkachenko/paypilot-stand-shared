""
import json
import uuid

from app import config, defects
from app.agent import pricing, prompt, summarize, tools
from app.agent.providers.base import get_provider
from app.tracing import RequestTrace

_sessions: dict[str, dict] = {}

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
    state = _sessions.setdefault(sid, {"messages": [], "steps": 0})
    return sid, state


def reset_sessions() -> None:
    _sessions.clear()


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


def run_turn(session_id: str | None, user_message: str) -> dict:
    sid, state = _session(session_id)
    state["steps"] += 1
    trace = RequestTrace(sid, state["steps"])
    provider = get_provider()
    system, prompt_version = prompt.build()
    trace.root.attributes["prompt.version"] = prompt_version
    trace.root.attributes["llm.provider"] = provider.name
    trace.root.attributes["context.replay_active"] = (
        defects.is_on("D15") and any(m["role"] == "tool" for m in state["messages"]))

    if summarize.should_summarize(state["steps"]) and not state.get("summarized"):
        with trace.span("agent.summarize") as s:
            summary = summarize.summarize_messages(provider, state["messages"])
            s.attributes.update({
                "summary.text": summary,
                "summary.replaced_messages": len(state["messages"]),
            })
        state["messages"] = [{"role": "user",
                              "content": f"(summary of earlier conversation)\n{summary}"}]
        state["summarized"] = True

    state["messages"].append({"role": "user", "content": user_message})
    answer = None
    total_in = total_out = 0
    model = getattr(provider, "model", "")
    call_costs: list[float | None] = []

    for step in range(config.MAX_AGENT_STEPS):
        with trace.span("llm.call", **{"agent.loop_step": step}) as s:
            resp = provider.complete(system, _messages_for_model(state),
                                     tools.specs())
            s.attributes.update({
                "gen_ai.request.model": resp.model,
                "gen_ai.usage.input_tokens": resp.input_tokens,
                "gen_ai.usage.output_tokens": resp.output_tokens,
            })
        total_in += resp.input_tokens
        total_out += resp.output_tokens
        model = resp.model or model
        call_costs.append(pricing.cost_usd(resp.model or model, resp.input_tokens,
                                           resp.output_tokens))

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
                total_in, total_out = _d14_retry_loop(
                    trace, provider, system, state, tc, total_in, total_out)
    else:
        answer = "I could not complete this request within the step budget."
        state["messages"].append({"role": "assistant", "content": answer})

    trace.root.attributes.update({
        "gen_ai.usage.total_input_tokens": total_in,
        "gen_ai.usage.total_output_tokens": total_out,
    })
    tree = trace.finish()
    return {"session_id": sid, "request_id": tree["request_id"],
            "answer": answer, "step_number": state["steps"],
            "prompt_version": prompt_version,
            "elapsed_ms": tree.get("duration_ms"),
            "usage": {"input_tokens": total_in, "output_tokens": total_out,
                      "model": model,
                      "cost_usd": pricing.total_cost_usd(call_costs)}}


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
                    tc: dict, total_in: int, total_out: int) -> tuple[int, int]:
    ""
    for attempt in range(1, _EMPTY_RETRY_LIMIT):
        with trace.span("llm.call", **{"agent.loop_step": f"retry-{attempt}",
                                       "retry.attempt": attempt}) as s:
            probe = provider.complete(system, _messages_for_model(state),
                                      tools.specs())
            s.attributes.update({
                "gen_ai.request.model": probe.model,
                "gen_ai.usage.input_tokens": probe.input_tokens,
                "gen_ai.usage.output_tokens": probe.output_tokens,
            })
        total_in += probe.input_tokens
        total_out += probe.output_tokens

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
    return total_in, total_out
