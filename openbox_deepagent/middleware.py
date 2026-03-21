"""OpenBox DeepAgents Middleware — LangChain AgentMiddleware for governance.

Replaces the astream_events-based handler with clean middleware hooks that fire
at exact execution points in the agent lifecycle:

    abefore_agent  → WorkflowStarted + SignalReceived + pre-screen guardrails
    awrap_model_call → LLMStarted (PII redaction) → Model → LLMCompleted
    awrap_tool_call  → ToolStarted → Tool (OTel spans) → ToolCompleted
    aafter_agent   → WorkflowCompleted + cleanup

Usage:
    from openbox_deepagent import create_openbox_middleware
    middleware = create_openbox_middleware(api_url=..., api_key=..., agent_name="Bot")
    agent = create_deep_agent(model="gpt-4o-mini", middleware=[middleware])
    result = await agent.ainvoke({"messages": [...]})
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, TYPE_CHECKING

from langchain.agents.middleware.types import AgentMiddleware, ModelRequest
from langgraph.prebuilt.tool_node import ToolCallRequest

from openbox_langgraph.client import GovernanceClient
from openbox_langgraph.config import GovernanceConfig, get_global_config, merge_config
from openbox_langgraph.types import GovernanceVerdictResponse

if TYPE_CHECKING:
    from langchain_core.messages import AIMessage, ToolMessage
    from langgraph.types import Command
    from openbox_langgraph.span_processor import WorkflowSpanProcessor

_logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# Options
# ═══════════════════════════════════════════════════════════════════

@dataclass
class OpenBoxMiddlewareOptions:
    """Configuration for OpenBoxMiddleware."""

    agent_name: str | None = None
    session_id: str | None = None
    task_queue: str = "langgraph"
    on_api_error: str = "fail_open"
    governance_timeout: float = 30.0
    known_subagents: list[str] = field(default_factory=lambda: ["general-purpose"])
    tool_type_map: dict[str, str] = field(default_factory=dict)
    skip_tool_types: set[str] = field(default_factory=set)
    sqlalchemy_engine: Any = None
    send_chain_start_event: bool = True
    send_chain_end_event: bool = True
    send_llm_start_event: bool = True
    send_llm_end_event: bool = True
    send_tool_start_event: bool = True
    send_tool_end_event: bool = True


# ═══════════════════════════════════════════════════════════════════
# OpenBoxMiddleware
# ═══════════════════════════════════════════════════════════════════

class OpenBoxMiddleware(AgentMiddleware):
    """LangChain AgentMiddleware implementing OpenBox governance for DeepAgents.

    Hooks map directly to the governance event lifecycle:
    - abefore_agent: session setup (WorkflowStarted, SignalReceived, pre-screen)
    - awrap_model_call: LLM governance (LLMStarted/Completed, PII redaction)
    - awrap_tool_call: tool governance (ToolStarted/Completed, SpanProcessor ctx)
    - aafter_agent: session close (WorkflowCompleted, cleanup)
    """

    def __init__(self, options: OpenBoxMiddlewareOptions | None = None) -> None:
        opts = options or OpenBoxMiddlewareOptions()
        self._options = opts

        # Build GovernanceConfig from options
        self._config: GovernanceConfig = merge_config({
            "on_api_error": opts.on_api_error,
            "api_timeout": opts.governance_timeout,
            "send_chain_start_event": opts.send_chain_start_event,
            "send_chain_end_event": opts.send_chain_end_event,
            "send_tool_start_event": opts.send_tool_start_event,
            "send_tool_end_event": opts.send_tool_end_event,
            "send_llm_start_event": opts.send_llm_start_event,
            "send_llm_end_event": opts.send_llm_end_event,
            "skip_tool_types": opts.skip_tool_types,
            "session_id": opts.session_id,
            "agent_name": opts.agent_name,
            "task_queue": opts.task_queue,
            "tool_type_map": opts.tool_type_map or {},
        })

        # Governance client
        gc = get_global_config()
        self._client = GovernanceClient(
            api_url=gc.api_url,
            api_key=gc.api_key,
            timeout=gc.governance_timeout,
            on_api_error=self._config.on_api_error,
        )

        # OTel span processor for hook-level governance
        self._span_processor: WorkflowSpanProcessor | None = None
        if gc and gc.api_url and gc.api_key:
            from openbox_langgraph.otel_setup import setup_opentelemetry_for_governance
            from openbox_langgraph.span_processor import WorkflowSpanProcessor as WSP
            self._span_processor = WSP()
            setup_opentelemetry_for_governance(
                span_processor=self._span_processor,
                api_url=gc.api_url,
                api_key=gc.api_key,
                ignored_urls=[gc.api_url],
                api_timeout=gc.governance_timeout,
                on_api_error=self._config.on_api_error,
                instrument_file_io=True,
                sqlalchemy_engine=opts.sqlalchemy_engine,
            )
            # Suppress harmless OTel context detach errors from asyncio.Task
            # boundaries in LangGraph — the token was attached in one task
            # but detached in another, which ContextVar rejects.
            logging.getLogger("opentelemetry.context").setLevel(logging.CRITICAL)
            _logger.debug("[OpenBox] OTel HTTP governance hooks enabled (middleware)")

        self._known_subagents: frozenset[str] = frozenset(opts.known_subagents)

        # Per-invocation state (reset in before_agent/abefore_agent)
        self._sync_mode: bool = False
        self._workflow_id: str = ""
        self._run_id: str = ""
        self._thread_id: str = ""
        self._pre_screen_response: GovernanceVerdictResponse | None = None
        self._first_llm_call: bool = True

    # ─────────────────────────────────────────────────────────────
    # Tool classification (ported from langgraph_handler.py)
    # ─────────────────────────────────────────────────────────────

    def _resolve_tool_type(self, tool_name: str, subagent_name: str | None) -> str | None:
        """Resolve semantic tool_type for a given tool.

        Priority: 1) explicit tool_type_map, 2) "a2a" if subagent, 3) None
        """
        if tool_name in self._config.tool_type_map:
            return self._config.tool_type_map[tool_name]
        if subagent_name:
            return "a2a"
        return None

    def _enrich_activity_input(
        self,
        base_input: list[Any] | None,
        tool_type: str | None,
        subagent_name: str | None,
    ) -> list[Any] | None:
        """Append __openbox metadata to activity_input for Rego policy use."""
        if tool_type is None and subagent_name is None:
            return base_input
        meta: dict[str, Any] = {}
        if tool_type is not None:
            meta["tool_type"] = tool_type
        if subagent_name is not None:
            meta["subagent_name"] = subagent_name
        result = list(base_input) if base_input else []
        result.append({"__openbox": meta})
        return result

    # ─────────────────────────────────────────────────────────────
    # Subagent introspection
    # ─────────────────────────────────────────────────────────────

    def get_known_subagents(self) -> list[str]:
        """Return the known subagent names registered with this middleware."""
        return sorted(self._known_subagents)

    # ─────────────────────────────────────────────────────────────
    # Async-to-sync bridge
    # ─────────────────────────────────────────────────────────────

    @staticmethod
    def _run_async(coro):
        """Run an async coroutine from sync context.

        When LangGraph calls sync hooks from inside its event loop,
        we must run in a thread pool. We copy the OTel context into
        the thread so span propagation works correctly.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop and loop.is_running():
            # Inside LangGraph's event loop — run in thread with OTel context
            import concurrent.futures
            from opentelemetry import context as otel_context
            ctx = otel_context.get_current()

            def _run_with_ctx():
                token = otel_context.attach(ctx)
                try:
                    return asyncio.run(coro)
                finally:
                    try:
                        otel_context.detach(token)
                    except Exception:
                        pass

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(_run_with_ctx).result()
        return asyncio.run(coro)

    # ─────────────────────────────────────────────────────────────
    # Sync middleware hooks — for invoke()/stream() callers
    # ─────────────────────────────────────────────────────────────

    def before_agent(self, state, runtime) -> dict[str, Any] | None:
        """Sync session setup: delegates to async via _run_async."""
        self._sync_mode = True
        if self._span_processor:
            self._span_processor.set_sync_mode(True)
        from openbox_deepagent.middleware_hooks import handle_before_agent
        return self._run_async(handle_before_agent(self, state, runtime))

    def after_agent(self, state, runtime) -> dict[str, Any] | None:
        """Sync session close: send WorkflowCompleted via sync httpx."""
        from openbox_deepagent.middleware_hooks import handle_after_agent
        self._run_async(handle_after_agent(self, state, runtime))
        return None

    def wrap_model_call(self, request: ModelRequest, handler) -> Any:
        """Sync LLM governance with direct OTel span in current thread.

        Creates OTel span and registers trace_id in the sync thread so httpx
        hooks can find the activity context (avoids asyncio.run ContextVar
        fragmentation).
        """
        from openbox_deepagent.middleware_hooks import handle_wrap_model_call
        from opentelemetry import context as otel_ctx, trace as otel_tr

        # Create OTel span in sync thread for httpx hook visibility
        tracer = otel_tr.get_tracer("openbox-deepagent")
        span = tracer.start_span("llm.call.sync", kind=otel_tr.SpanKind.INTERNAL)
        token = otel_ctx.attach(otel_tr.set_span_in_context(span))
        trace_id = span.get_span_context().trace_id
        activity_id = None

        try:
            # Run the async governance handler — it will register its own
            # trace_id via _run_with_otel_context, but we also register the
            # sync thread's trace_id so httpx sync hooks can find it
            async def async_handler(req):
                return handler(req)

            async def _wrapped():
                nonlocal activity_id
                # Import here to get the activity_id from the handler
                import uuid
                activity_id = str(uuid.uuid4())
                if self._span_processor and trace_id:
                    self._span_processor.register_trace(trace_id, self._workflow_id, activity_id)
                    self._span_processor.set_activity_context(self._workflow_id, activity_id, {
                        "source": "workflow-telemetry",
                        "workflow_id": self._workflow_id,
                        "run_id": self._run_id,
                        "event_type": "ActivityStarted",
                        "activity_id": activity_id,
                        "activity_type": "llm_call",
                    })
                return await handle_wrap_model_call(self, request, async_handler)

            return self._run_async(_wrapped())
        finally:
            span.end()
            try:
                otel_ctx.detach(token)
            except Exception:
                pass

    def wrap_tool_call(self, request: ToolCallRequest, handler) -> Any:
        """Sync tool governance with direct OTel span in current thread."""
        from openbox_deepagent.middleware_hooks import handle_wrap_tool_call
        from opentelemetry import context as otel_ctx, trace as otel_tr

        tracer = otel_tr.get_tracer("openbox-deepagent")
        tool_name = request.tool_call.get("name", "tool") if hasattr(request, "tool_call") else "tool"
        span = tracer.start_span(f"tool.{tool_name}.sync", kind=otel_tr.SpanKind.INTERNAL)
        token = otel_ctx.attach(otel_tr.set_span_in_context(span))
        trace_id = span.get_span_context().trace_id

        try:
            async def async_handler(req):
                return handler(req)

            # Register sync thread trace_id before running handler
            if self._span_processor and trace_id:
                import uuid
                sync_activity_id = str(uuid.uuid4())
                self._span_processor.register_trace(trace_id, self._workflow_id, sync_activity_id)
                self._span_processor.set_activity_context(self._workflow_id, sync_activity_id, {
                    "source": "workflow-telemetry",
                    "workflow_id": self._workflow_id,
                    "run_id": self._run_id,
                    "event_type": "ActivityStarted",
                    "activity_id": sync_activity_id,
                    "activity_type": tool_name,
                })

            return self._run_async(handle_wrap_tool_call(self, request, async_handler))
        finally:
            span.end()
            try:
                otel_ctx.detach(token)
            except Exception:
                pass

    # ─────────────────────────────────────────────────────────────
    # Async middleware hooks — for ainvoke()/astream() callers
    # ─────────────────────────────────────────────────────────────

    async def abefore_agent(self, state, runtime) -> dict[str, Any] | None:
        """Session setup: WorkflowStarted + SignalReceived + pre-screen guardrails."""
        self._sync_mode = False
        if self._span_processor:
            self._span_processor.set_sync_mode(False)
        from openbox_deepagent.middleware_hooks import handle_before_agent
        return await handle_before_agent(self, state, runtime)

    async def aafter_agent(self, state, runtime) -> dict[str, Any] | None:
        """Session close: WorkflowCompleted + cleanup."""
        from openbox_deepagent.middleware_hooks import handle_after_agent
        return await handle_after_agent(self, state, runtime)

    async def awrap_model_call(self, request: ModelRequest, handler) -> Any:
        """LLM governance: LLMStarted → PII redaction → Model → LLMCompleted."""
        from openbox_deepagent.middleware_hooks import handle_wrap_model_call
        return await handle_wrap_model_call(self, request, handler)

    async def awrap_tool_call(self, request: ToolCallRequest, handler) -> Any:
        """Tool governance: ToolStarted → Tool (OTel spans) → ToolCompleted."""
        from openbox_deepagent.middleware_hooks import handle_wrap_tool_call
        return await handle_wrap_tool_call(self, request, handler)
