"""Hook implementations for OpenBoxMiddleware.

Each function implements one middleware hook, mapping to governance events:
- handle_before_agent  → SignalReceived + WorkflowStarted + pre-screen LLMStarted
- handle_after_agent   → WorkflowCompleted + cleanup
- handle_wrap_model_call → LLMStarted (PII redaction) → Model → LLMCompleted
- handle_wrap_tool_call  → ToolStarted → Tool (OTel spans) → ToolCompleted
"""

from __future__ import annotations

import time
import uuid
from typing import Any, TYPE_CHECKING

import logging

from opentelemetry import context as otel_context, trace as otel_trace

_tracer = otel_trace.get_tracer("openbox-deepagent")
_logger = logging.getLogger(__name__)

from openbox_langgraph.errors import (
    ApprovalExpiredError,
    ApprovalRejectedError,
    GovernanceBlockedError,
    GovernanceHaltError,
    GuardrailsValidationError,
)
from openbox_langgraph.hitl import HITLPollParams, poll_until_decision
from openbox_langgraph.types import (
    LangChainGovernanceEvent,
    rfc3339_now,
    safe_serialize,
)
from openbox_langgraph.verdict_handler import enforce_verdict

from openbox_deepagent.subagent_resolver import (
    DEEPAGENT_SUBAGENT_TOOL,
    resolve_subagent_from_tool_call,
)

if TYPE_CHECKING:
    from openbox_deepagent.middleware import OpenBoxMiddleware


def _extract_governance_blocked(exc: Exception) -> GovernanceBlockedError | None:
    """Walk exception chain to find a wrapped GovernanceBlockedError.

    LLM SDKs (OpenAI, Anthropic) wrap httpx errors. When an OTel hook raises
    GovernanceBlockedError inside httpx, the LLM SDK wraps it as APIConnectionError.
    This function unwraps the chain via __cause__ / __context__ to recover it.
    """
    cause: BaseException | None = exc
    seen: set[int] = set()
    while cause is not None:
        if id(cause) in seen:
            break
        seen.add(id(cause))
        if isinstance(cause, GovernanceBlockedError):
            return cause
        cause = getattr(cause, '__cause__', None) or getattr(cause, '__context__', None)
    return None


# ═══════════════════════════════════════════════════════════════════
# Helper: evaluate event (sync or async based on mode)
# ═══════════════════════════════════════════════════════════════════

async def _evaluate(mw: OpenBoxMiddleware, event: Any) -> Any:
    """Send governance event using sync httpx.Client when in sync mode,
    async httpx.AsyncClient otherwise. Prevents context cancellation
    caused by asyncio.run() teardown in sync-to-async bridge."""
    if mw._sync_mode:
        return mw._client.evaluate_event_sync(event)
    return await mw._client.evaluate_event(event)


async def _poll_approval_or_halt(
    mw: OpenBoxMiddleware,
    activity_id: str,
    activity_type: str,
) -> None:
    """Poll for HITL approval, clearing abort state first.

    On rejection/expiry, clears SpanProcessor context and raises GovernanceHaltError.
    On approval, returns normally so the caller can retry.
    """
    if mw._span_processor:
        mw._span_processor.clear_activity_abort(mw._workflow_id, activity_id)
    try:
        await poll_until_decision(
            mw._client,
            HITLPollParams(
                workflow_id=mw._workflow_id, run_id=mw._run_id,
                activity_id=activity_id, activity_type=activity_type,
            ),
            mw._config.hitl,
        )
    except (ApprovalRejectedError, ApprovalExpiredError) as e:
        if mw._span_processor:
            mw._span_processor.clear_activity_context(mw._workflow_id, activity_id)
        raise GovernanceHaltError(str(e)) from e


# ═══════════════════════════════════════════════════════════════════
# Helper: build base governance event fields
# ═══════════════════════════════════════════════════════════════════

def _base_event_fields(mw: OpenBoxMiddleware) -> dict[str, Any]:
    """Return common fields for all governance events."""
    return {
        "source": "workflow-telemetry",
        "workflow_id": mw._workflow_id,
        "run_id": mw._run_id,
        "workflow_type": mw._config.agent_name or "LangGraphRun",
        "task_queue": mw._config.task_queue,
        "timestamp": rfc3339_now(),
        "session_id": mw._config.session_id,
    }


# ═══════════════════════════════════════════════════════════════════
# Helper: extract last user message from state
# ═══════════════════════════════════════════════════════════════════

def _extract_last_user_message(messages: list[Any]) -> str | None:
    """Extract the last human/user message text from agent state messages."""
    for msg in reversed(messages):
        if isinstance(msg, dict):
            if msg.get("role") in ("user", "human"):
                content = msg.get("content")
                return content if isinstance(content, str) else None
        elif hasattr(msg, "type") and msg.type in ("human", "generic"):
            content = msg.content
            return content if isinstance(content, str) else None
    return None


# ═══════════════════════════════════════════════════════════════════
# Helper: extract prompt from LangChain messages
# ═══════════════════════════════════════════════════════════════════

def _extract_prompt_from_messages(messages: Any) -> str:
    """Extract human/user message text from a messages list."""
    if not isinstance(messages, (list, tuple)):
        return ""
    parts: list[str] = []
    for msg in messages:
        # Nested list of messages
        if isinstance(msg, (list, tuple)):
            for inner in msg:
                _append_human_content(inner, parts)
        else:
            _append_human_content(msg, parts)
    return "\n".join(parts)


def _append_human_content(msg: Any, parts: list[str]) -> None:
    """Append human message content to parts list."""
    role = None
    content = None
    if hasattr(msg, "type"):
        role = msg.type
        content = msg.content
    elif isinstance(msg, dict):
        role = msg.get("role") or msg.get("type", "")
        content = msg.get("content", "")
    if role not in ("human", "user", "generic"):
        return
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(part.get("text", ""))


# ═══════════════════════════════════════════════════════════════════
# Helper: PII redaction
# ═══════════════════════════════════════════════════════════════════

def _apply_pii_redaction(messages: list[Any], redacted_input: Any) -> None:
    """Apply PII redaction to messages in-place from guardrails response."""
    # Extract redacted text from Core's format: [{"prompt": "..."}] or string
    redacted_text = None
    if isinstance(redacted_input, list) and redacted_input:
        first = redacted_input[0]
        if isinstance(first, dict):
            redacted_text = first.get("prompt")
        elif isinstance(first, str):
            redacted_text = first
    elif isinstance(redacted_input, str):
        redacted_text = redacted_input

    if not redacted_text:
        return

    # Replace the last human message in the list
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if hasattr(msg, "type") and msg.type in ("human", "generic"):
            msg.content = redacted_text
            break
        elif isinstance(msg, dict) and msg.get("role") in ("user", "human"):
            msg["content"] = redacted_text
            break


# ═══════════════════════════════════════════════════════════════════
# Helper: extract token usage from model response
# ═══════════════════════════════════════════════════════════════════

def _extract_response_metadata(response: Any) -> dict[str, Any]:
    """Extract tokens, model name, completion, tool_calls from model response."""
    result: dict[str, Any] = {}

    # Try to get the AIMessage from ModelResponse or directly
    ai_msg = response
    if hasattr(response, "message"):
        ai_msg = response.message

    # Model name
    if hasattr(ai_msg, "response_metadata"):
        meta = ai_msg.response_metadata or {}
        result["llm_model"] = meta.get("model_name") or meta.get("model")

    # Token usage
    usage = getattr(ai_msg, "usage_metadata", None) or {}
    if isinstance(usage, dict):
        result["input_tokens"] = usage.get("input_tokens") or usage.get("prompt_tokens")
        result["output_tokens"] = usage.get("output_tokens") or usage.get("completion_tokens")
        inp = result.get("input_tokens") or 0
        out = result.get("output_tokens") or 0
        result["total_tokens"] = inp + out if (inp or out) else None

    # Completion text
    content = getattr(ai_msg, "content", None)
    if isinstance(content, str):
        result["completion"] = content
    elif isinstance(content, list):
        parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
        result["completion"] = " ".join(parts) if parts else None

    # Tool calls
    tool_calls = getattr(ai_msg, "tool_calls", None) or []
    result["has_tool_calls"] = bool(tool_calls)

    return result


# ═══════════════════════════════════════════════════════════════════
# Helper: OTel context propagation across asyncio.Task boundaries
# ═══════════════════════════════════════════════════════════════════

async def _run_with_otel_context(
    mw: OpenBoxMiddleware,
    span_name: str,
    activity_id: str,
    handler: Any,
    request: Any,
) -> Any:
    """Execute handler inside an explicit OTel span to propagate trace context.

    LangGraph spawns asyncio.Tasks for tool/LLM execution. OTel trace context
    breaks at Task boundaries — child spans get new trace_ids.

    We manually manage attach/detach instead of using `start_as_current_span`
    context manager because the `await handler(request)` may cross asyncio Task
    boundaries, causing the detach token to be invalid in the new Task context.
    The detach error is harmless but noisy — suppressing it here.
    """
    parent_ctx = otel_context.get_current()
    span = _tracer.start_span(span_name, context=parent_ctx, kind=otel_trace.SpanKind.INTERNAL)
    token = otel_context.attach(otel_trace.set_span_in_context(span, parent_ctx))

    trace_id = span.get_span_context().trace_id
    if mw._span_processor and trace_id:
        mw._span_processor.register_trace(trace_id, mw._workflow_id, activity_id)

    try:
        result = await handler(request)
        return result
    finally:
        span.end()
        try:
            otel_context.detach(token)
        except Exception:
            pass  # Token created in different asyncio context — safe to ignore


# ═══════════════════════════════════════════════════════════════════
# Hook: abefore_agent
# ═══════════════════════════════════════════════════════════════════

async def handle_before_agent(mw: OpenBoxMiddleware, state: Any, runtime: Any) -> dict[str, Any] | None:
    """Session setup: SignalReceived + WorkflowStarted + pre-screen guardrails.

    Fires once per invoke() before any model calls.
    """
    # 1. Extract thread_id and generate fresh session IDs
    config = getattr(runtime, "config", None) or {}
    configurable = config.get("configurable", {}) if isinstance(config, dict) else {}
    mw._thread_id = configurable.get("thread_id", "deepagents")
    _turn = uuid.uuid4().hex
    mw._workflow_id = f"{mw._thread_id}-{_turn[:8]}"
    mw._run_id = f"{mw._thread_id}-run-{_turn[8:16]}"
    mw._first_llm_call = True
    mw._pre_screen_response = None

    base = _base_event_fields(mw)
    messages = state.get("messages", []) if isinstance(state, dict) else getattr(state, "messages", [])

    # 2. SignalReceived — user prompt as trigger
    user_prompt = _extract_last_user_message(messages)
    if user_prompt:
        sig_event = LangChainGovernanceEvent(
            **base,
            event_type="SignalReceived",
            activity_id=f"{mw._run_id}-sig",
            activity_type="user_prompt",
            signal_name="user_prompt",
            signal_args=[user_prompt],
        )
        await _evaluate(mw,sig_event)

    # 3. WorkflowStarted
    if mw._config.send_chain_start_event:
        wf_event = LangChainGovernanceEvent(
            **base,
            event_type="WorkflowStarted",
            activity_id=f"{mw._run_id}-wf",
            activity_type=mw._config.agent_name or "LangGraphRun",
            activity_input=[safe_serialize(state)],
        )
        await _evaluate(mw,wf_event)

    # 4. Pre-screen LLMStarted (guardrails on user prompt)
    if mw._config.send_llm_start_event and user_prompt and user_prompt.strip():
        gov = LangChainGovernanceEvent(
            **base,
            event_type="LLMStarted",
            activity_id=f"{mw._run_id}-pre",
            activity_type="llm_call",
            activity_input=[{"prompt": user_prompt}],
            prompt=user_prompt,
        )
        response = await _evaluate(mw,gov)

        if response is not None:
            # Enforce — BLOCK/HALT raises immediately
            enforcement_error: Exception | None = None
            try:
                result = enforce_verdict(response, "llm_start")
            except Exception as exc:
                enforcement_error = exc

            # Close workflow on enforcement error
            if enforcement_error is not None and mw._config.send_chain_end_event:
                wf_end = LangChainGovernanceEvent(
                    **_base_event_fields(mw),
                    event_type="WorkflowCompleted",
                    activity_id=f"{mw._run_id}-wf",
                    activity_type=mw._config.agent_name or "LangGraphRun",
                    status="failed",
                    error=str(enforcement_error),
                )
                await _evaluate(mw,wf_end)
                raise enforcement_error

            # HITL polling if needed
            if result and result.requires_hitl:
                try:
                    await poll_until_decision(
                        mw._client,
                        HITLPollParams(
                            workflow_id=mw._workflow_id,
                            run_id=mw._run_id,
                            activity_id=f"{mw._run_id}-pre",
                            activity_type="llm_call",
                        ),
                        mw._config.hitl,
                    )
                except (ApprovalRejectedError, ApprovalExpiredError) as e:
                    raise GovernanceHaltError(str(e)) from e

        mw._pre_screen_response = response

    return None


# ═══════════════════════════════════════════════════════════════════
# Hook: aafter_agent
# ═══════════════════════════════════════════════════════════════════

async def handle_after_agent(mw: OpenBoxMiddleware, state: Any, runtime: Any) -> dict[str, Any] | None:
    """Session close: WorkflowCompleted + cleanup.

    Fires once per invoke() after agent completes.
    """
    if mw._config.send_chain_end_event:
        messages = state.get("messages", []) if isinstance(state, dict) else getattr(state, "messages", [])
        last_content = None
        if messages:
            last_msg = messages[-1]
            last_content = getattr(last_msg, "content", None) if hasattr(last_msg, "content") else (
                last_msg.get("content") if isinstance(last_msg, dict) else None
            )

        wf_event = LangChainGovernanceEvent(
            **_base_event_fields(mw),
            event_type="WorkflowCompleted",
            activity_id=f"{mw._run_id}-wf",
            activity_type=mw._config.agent_name or "LangGraphRun",
            workflow_output=safe_serialize({"result": last_content}),
            status="completed",
        )
        await _evaluate(mw,wf_event)

    # Cleanup SpanProcessor state
    if mw._span_processor:
        mw._span_processor.unregister_workflow(mw._workflow_id)

    return None


# ═══════════════════════════════════════════════════════════════════
# Hook: awrap_model_call
# ═══════════════════════════════════════════════════════════════════

async def handle_wrap_model_call(mw: OpenBoxMiddleware, request: Any, handler: Any) -> Any:
    """LLM governance: LLMStarted → PII redaction → Model → LLMCompleted.

    Wraps each LLM call within the agent loop.
    """
    # 1. Extract prompt from request messages
    prompt_text = _extract_prompt_from_messages(request.messages)

    # 2. Skip governance for empty prompts (subagent internal LLMs)
    if not prompt_text.strip():
        return await handler(request)

    base = _base_event_fields(mw)
    activity_id = str(uuid.uuid4())

    # 3. LLMStarted — reuse pre_screen for first call
    if mw._first_llm_call and mw._pre_screen_response is not None:
        response = mw._pre_screen_response
        mw._pre_screen_response = None
        mw._first_llm_call = False
        activity_id = f"{mw._run_id}-pre"
    else:
        mw._first_llm_call = False
        if mw._config.send_llm_start_event:
            model_name = str(request.model) if hasattr(request, "model") and request.model else "LLM"
            gov = LangChainGovernanceEvent(
                **base,
                event_type="LLMStarted",
                activity_id=activity_id,
                activity_type="llm_call",
                activity_input=[{"prompt": prompt_text}],
                llm_model=model_name,
                prompt=prompt_text,
            )
            response = await _evaluate(mw,gov)
        else:
            response = None

    # 4. Apply PII redaction to request messages
    if response and response.guardrails_result:
        gr = response.guardrails_result
        if gr.input_type == "activity_input" and gr.redacted_input is not None:
            _apply_pii_redaction(request.messages, gr.redacted_input)

    # 5. Register SpanProcessor context for LLM call
    if mw._span_processor:
        mw._span_processor.set_activity_context(mw._workflow_id, activity_id, {
            **base,
            "event_type": "ActivityStarted",
            "activity_id": activity_id,
            "activity_type": "llm_call",
        })

    # 6. Execute model call (OTel span bridges asyncio.Task boundary)
    #    Retry loop: hooks may return REQUIRE_APPROVAL multiple times (different
    #    span types). Each approval triggers a retry. No client-side deadline.
    start = time.monotonic()
    while True:
        try:
            model_response = await _run_with_otel_context(
                mw, "llm.call", activity_id, handler, request,
            )
            break  # success
        except GovernanceBlockedError as hook_err:
            if hook_err.verdict != "require_approval":
                raise
            _logger.info("[OpenBox] Hook REQUIRE_APPROVAL during activity=llm_call, polling")
            await _poll_approval_or_halt(mw, activity_id, "llm_call")
            _logger.info("[OpenBox] Approval granted, retrying activity=llm_call")
        except Exception as exc:
            hook_err = _extract_governance_blocked(exc)
            if hook_err is None or hook_err.verdict != "require_approval":
                raise
            _logger.info("[OpenBox] Hook REQUIRE_APPROVAL (wrapped) during activity=llm_call, polling")
            await _poll_approval_or_halt(mw, activity_id, "llm_call")
            _logger.info("[OpenBox] Approval granted, retrying activity=llm_call")
    duration_ms = (time.monotonic() - start) * 1000

    # 7. Send LLMCompleted
    _logger.debug("[OpenBox] wrap_model_call AFTER: activity_id=%s duration=%.0fms send_llm_end=%s", activity_id, duration_ms, mw._config.send_llm_end_event)
    if mw._config.send_llm_end_event:
        meta = _extract_response_metadata(model_response)
        completed = LangChainGovernanceEvent(
            **_base_event_fields(mw),
            event_type="LLMCompleted",
            activity_id=f"{activity_id}-c",
            activity_type="llm_call",
            activity_output=safe_serialize(model_response) if hasattr(model_response, "__dict__") else None,
            status="completed",
            duration_ms=duration_ms,
            llm_model=meta.get("llm_model"),
            input_tokens=meta.get("input_tokens"),
            output_tokens=meta.get("output_tokens"),
            total_tokens=meta.get("total_tokens"),
            has_tool_calls=meta.get("has_tool_calls"),
            completion=meta.get("completion"),
        )
        _logger.debug("[OpenBox] LLMCompleted SENDING: activity_id=%s-c", activity_id)
        resp = await _evaluate(mw,completed)
        _logger.debug("[OpenBox] LLMCompleted SENT: activity_id=%s-c resp=%s", activity_id, resp)
        if resp is not None:
            enforce_verdict(resp, "llm_end")

    # 8. Clear SpanProcessor context
    if mw._span_processor:
        mw._span_processor.clear_activity_context(mw._workflow_id, activity_id)

    return model_response


# ═══════════════════════════════════════════════════════════════════
# Hook: awrap_tool_call (Process 2 — core of diagram)
# ═══════════════════════════════════════════════════════════════════

async def handle_wrap_tool_call(mw: OpenBoxMiddleware, request: Any, handler: Any) -> Any:
    """Tool governance: ToolStarted → Tool (OTel spans) → ToolCompleted.

    Wraps each tool execution. Manages SpanProcessor context for OTel span
    capture during tool execution (HTTP/DB/file governance hooks).
    """
    tool_name = request.tool_call["name"]
    tool_args = request.tool_call.get("args", {})

    # 1. Skip if in skip_tool_types
    if tool_name in (mw._config.skip_tool_types or set()):
        return await handler(request)

    # 2. Detect subagent
    subagent_name = resolve_subagent_from_tool_call(tool_name, tool_args)

    # 3. Classify tool and build enriched input
    activity_id = str(uuid.uuid4())
    tool_type = mw._resolve_tool_type(tool_name, subagent_name)
    enriched_input = mw._enrich_activity_input(
        [safe_serialize(tool_args)], tool_type, subagent_name
    )

    base = _base_event_fields(mw)

    # === BEFORE TOOL CALL ===

    # 4. Register SpanProcessor context for all tools (including subagents)
    # Subagent internal HTTP/DB/file calls should trigger hook-level governance
    if mw._span_processor:
        activity_context = {
            **base,
            "event_type": "ActivityStarted",
            "activity_id": activity_id,
            "activity_type": tool_name,
        }
        mw._span_processor.set_activity_context(mw._workflow_id, activity_id, activity_context)

    # 5. Send ToolStarted + enforce verdict
    _logger.debug("[OpenBox] ToolStarted SENDING: tool=%s activity_id=%s tool_type=%s subagent=%s",
                  tool_name, activity_id, tool_type, subagent_name)
    if mw._config.send_tool_start_event:
        gov = LangChainGovernanceEvent(
            **base,
            event_type="ToolStarted",
            activity_id=activity_id,
            activity_type=tool_name,
            activity_input=enriched_input,
            tool_name=tool_name,
            tool_type=tool_type,
            tool_input=safe_serialize(tool_args),
            subagent_name=subagent_name,
        )
        response = await _evaluate(mw,gov)
        if response is not None:
            result = enforce_verdict(response, "tool_start")
            if result.requires_hitl:
                try:
                    await poll_until_decision(
                        mw._client,
                        HITLPollParams(
                            workflow_id=mw._workflow_id,
                            run_id=mw._run_id,
                            activity_id=activity_id,
                            activity_type=tool_name,
                        ),
                        mw._config.hitl,
                    )
                except (ApprovalRejectedError, ApprovalExpiredError) as e:
                    # Clear SpanProcessor before raising
                    if mw._span_processor:
                        mw._span_processor.clear_activity_context(mw._workflow_id, activity_id)
                    raise GovernanceHaltError(str(e)) from e

    # === TOOL CALL (OTel span bridges asyncio.Task boundary) ===
    # Retry loop: if a hook returns REQUIRE_APPROVAL, poll for approval and retry.
    # Loops until the tool succeeds or a non-approval error occurs.
    # poll_until_decision has no deadline — OpenBox server controls expiration.

    start = time.monotonic()
    while True:
        try:
            tool_result = await _run_with_otel_context(
                mw, f"tool.{tool_name}", activity_id, handler, request,
            )
            break  # success — exit retry loop
        except GovernanceBlockedError as hook_err:
            if hook_err.verdict != "require_approval":
                _logger.warning("[OpenBox] Hook BLOCKED tool=%s verdict=%s", tool_name, hook_err.verdict)
                duration_ms = (time.monotonic() - start) * 1000
                if mw._span_processor:
                    mw._span_processor.clear_activity_context(mw._workflow_id, activity_id)
                if mw._config.send_tool_end_event:
                    failed_event = LangChainGovernanceEvent(
                        **_base_event_fields(mw),
                        event_type="ToolCompleted",
                        activity_id=f"{activity_id}-c",
                        activity_type=tool_name,
                        activity_output=safe_serialize({"error": str(hook_err)}),
                        tool_name=tool_name,
                        tool_type=tool_type,
                        subagent_name=subagent_name,
                        status="failed",
                        duration_ms=duration_ms,
                    )
                    await _evaluate(mw, failed_event)
                raise

            _logger.info("[OpenBox] Hook REQUIRE_APPROVAL during activity=%s, polling", tool_name)
            await _poll_approval_or_halt(mw, activity_id, tool_name)
            _logger.info("[OpenBox] Approval granted, retrying activity=%s", tool_name)

        except Exception as exc:
            hook_err = _extract_governance_blocked(exc)
            if hook_err is not None and hook_err.verdict == "require_approval":
                _logger.info("[OpenBox] Hook REQUIRE_APPROVAL (wrapped) during activity=%s, polling", tool_name)
                await _poll_approval_or_halt(mw, activity_id, tool_name)
                _logger.info("[OpenBox] Approval granted, retrying activity=%s", tool_name)
            else:
                _logger.warning("[OpenBox] wrap_tool_call EXCEPTION: tool=%s activity_id=%s error=%s", tool_name, activity_id, exc)
                duration_ms = (time.monotonic() - start) * 1000
                if mw._span_processor:
                    mw._span_processor.clear_activity_context(mw._workflow_id, activity_id)
                if mw._config.send_tool_end_event:
                    failed_event = LangChainGovernanceEvent(
                        **_base_event_fields(mw),
                        event_type="ToolCompleted",
                        activity_id=f"{activity_id}-c",
                        activity_type=tool_name,
                        activity_output=safe_serialize({"error": str(exc)}),
                        tool_name=tool_name,
                        tool_type=tool_type,
                        subagent_name=subagent_name,
                        status="failed",
                        duration_ms=duration_ms,
                    )
                    await _evaluate(mw, failed_event)
                raise
    duration_ms = (time.monotonic() - start) * 1000
    _logger.debug("[OpenBox] wrap_tool_call AFTER: tool=%s activity_id=%s duration=%.0fms send_tool_end=%s",
                  tool_name, activity_id, duration_ms, mw._config.send_tool_end_event)

    # === AFTER TOOL CALL ===

    # 6. Clear SpanProcessor context
    if mw._span_processor:
        mw._span_processor.clear_activity_context(mw._workflow_id, activity_id)

    # 7. Send ToolCompleted + enforce verdict
    _logger.debug("[OpenBox] ToolCompleted PREPARING: tool=%s activity_id=%s-c", tool_name, activity_id)
    if mw._config.send_tool_end_event:
        try:
            serialized_output = (
                safe_serialize({"result": tool_result})
                if isinstance(tool_result, str)
                else safe_serialize(tool_result)
            )
        except Exception:
            serialized_output = {"result": str(tool_result)}
        completed = LangChainGovernanceEvent(
            **_base_event_fields(mw),
            event_type="ToolCompleted",
            activity_id=f"{activity_id}-c",
            activity_type=tool_name,
            activity_output=serialized_output,
            tool_name=tool_name,
            tool_type=tool_type,
            subagent_name=subagent_name,
            status="completed",
            duration_ms=duration_ms,
        )
        _logger.debug("[OpenBox] ToolCompleted SENDING: tool=%s activity_id=%s-c", tool_name, activity_id)
        resp = await _evaluate(mw,completed)
        _logger.debug("[OpenBox] ToolCompleted SENT: tool=%s activity_id=%s-c resp=%s", tool_name, activity_id, resp)
        if resp is not None:
            result = enforce_verdict(resp, "tool_end")
            if result.requires_hitl:
                try:
                    await poll_until_decision(
                        mw._client,
                        HITLPollParams(
                            workflow_id=mw._workflow_id,
                            run_id=mw._run_id,
                            activity_id=f"{activity_id}-c",
                            activity_type=tool_name,
                        ),
                        mw._config.hitl,
                    )
                except (ApprovalRejectedError, ApprovalExpiredError) as e:
                    raise GovernanceHaltError(str(e)) from e

    return tool_result
