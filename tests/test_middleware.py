"""Tests for OpenBoxMiddleware — LangChain AgentMiddleware for governance."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openbox_deepagent.middleware import OpenBoxMiddleware, OpenBoxMiddlewareOptions
from openbox_deepagent.middleware_hooks import (
    _extract_last_user_message,
    _extract_prompt_from_messages,
    _extract_response_metadata,
    _run_with_otel_context,
    handle_after_agent,
    handle_before_agent,
    handle_wrap_model_call,
    handle_wrap_tool_call,
)
from openbox_langgraph.types import GovernanceVerdictResponse, Verdict


# ═══════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture
def mock_client():
    """Mock GovernanceClient that returns ALLOW verdict."""
    client = AsyncMock()
    client.evaluate_event = AsyncMock(return_value=GovernanceVerdictResponse(
        verdict=Verdict.ALLOW,
    ))
    return client


@pytest.fixture
def mock_span_processor():
    """Mock WorkflowSpanProcessor."""
    sp = MagicMock()
    sp.set_activity_context = MagicMock()
    sp.clear_activity_context = MagicMock()
    sp.register_trace = MagicMock()
    sp.unregister_workflow = MagicMock()
    return sp


@pytest.fixture
def middleware(mock_client, mock_span_processor):
    """OpenBoxMiddleware with mocked dependencies."""
    with patch("openbox_deepagent.middleware.get_global_config") as mock_gc, \
         patch("openbox_deepagent.middleware.merge_config") as mock_mc:
        mock_gc.return_value = MagicMock(
            api_url="http://test", api_key="obx_test_key",
            governance_timeout=30.0,
        )
        # merge_config returns a config-like object with all necessary attrs
        config = MagicMock()
        config.agent_name = "TestBot"
        config.session_id = None
        config.task_queue = "langgraph"
        config.on_api_error = "fail_open"
        config.send_chain_start_event = True
        config.send_chain_end_event = True
        config.send_llm_start_event = True
        config.send_llm_end_event = True
        config.send_tool_start_event = True
        config.send_tool_end_event = True
        config.skip_tool_types = set()
        config.tool_type_map = {"search_web": "http"}
        config.hitl = MagicMock(enabled=False, skip_tool_types=set())
        mock_mc.return_value = config

        mw = OpenBoxMiddleware(OpenBoxMiddlewareOptions(
            agent_name="TestBot",
            known_subagents=["general-purpose", "researcher"],
            tool_type_map={"search_web": "http"},
        ))
    mw._client = mock_client
    mw._span_processor = mock_span_processor
    return mw


@pytest.fixture
def runtime():
    """Mock Runtime with configurable thread_id."""
    rt = MagicMock()
    rt.config = {"configurable": {"thread_id": "test-thread-42"}}
    return rt


@pytest.fixture
def state_with_user_msg():
    """Agent state with a user message."""
    return {"messages": [
        MagicMock(type="human", content="Research quantum computing"),
    ]}


# ═══════════════════════════════════════════════════════════════════
# Construction tests
# ═══════════════════════════════════════════════════════════════════

class TestConstruction:
    def test_defaults(self, middleware):
        assert middleware._known_subagents == frozenset(["general-purpose", "researcher"])
        assert middleware.get_known_subagents() == ["general-purpose", "researcher"]

    def test_get_known_subagents_sorted(self, middleware):
        assert middleware.get_known_subagents() == sorted(["general-purpose", "researcher"])


# ═══════════════════════════════════════════════════════════════════
# Tool classification tests
# ═══════════════════════════════════════════════════════════════════

class TestToolClassification:
    def test_resolve_tool_type_from_map(self, middleware):
        assert middleware._resolve_tool_type("search_web", None) == "http"

    def test_resolve_tool_type_subagent(self, middleware):
        assert middleware._resolve_tool_type("task", "researcher") == "a2a"

    def test_resolve_tool_type_unknown(self, middleware):
        assert middleware._resolve_tool_type("my_tool", None) is None

    def test_enrich_activity_input_with_type(self, middleware):
        result = middleware._enrich_activity_input([{"query": "test"}], "http", None)
        assert result[-1] == {"__openbox": {"tool_type": "http"}}

    def test_enrich_activity_input_with_subagent(self, middleware):
        result = middleware._enrich_activity_input([{"desc": "do it"}], "a2a", "writer")
        assert result[-1] == {"__openbox": {"tool_type": "a2a", "subagent_name": "writer"}}

    def test_enrich_activity_input_no_metadata(self, middleware):
        base = [{"query": "test"}]
        result = middleware._enrich_activity_input(base, None, None)
        assert result is base  # unchanged


# ═══════════════════════════════════════════════════════════════════
# Helper tests
# ═══════════════════════════════════════════════════════════════════

class TestHelpers:
    def test_extract_last_user_message_dict(self):
        msgs = [{"role": "user", "content": "hello"}]
        assert _extract_last_user_message(msgs) == "hello"

    def test_extract_last_user_message_object(self):
        msg = MagicMock(type="human", content="hello world")
        assert _extract_last_user_message([msg]) == "hello world"

    def test_extract_last_user_message_empty(self):
        assert _extract_last_user_message([]) is None

    def test_extract_prompt_from_messages(self):
        msgs = [MagicMock(type="human", content="prompt text")]
        assert _extract_prompt_from_messages(msgs) == "prompt text"

    def test_extract_prompt_from_messages_empty(self):
        assert _extract_prompt_from_messages([]) == ""

    def test_extract_prompt_skips_non_human(self):
        msgs = [MagicMock(type="ai", content="response")]
        assert _extract_prompt_from_messages(msgs) == ""


# ═══════════════════════════════════════════════════════════════════
# abefore_agent tests
# ═══════════════════════════════════════════════════════════════════

class TestBeforeAgent:
    @pytest.mark.asyncio
    async def test_sends_signal_received(self, middleware, state_with_user_msg, runtime):
        await handle_before_agent(middleware, state_with_user_msg, runtime)
        calls = middleware._client.evaluate_event.call_args_list
        # First call is SignalReceived
        sig_event = calls[0][0][0]
        assert sig_event.event_type == "SignalReceived"
        assert sig_event.signal_name == "user_prompt"
        assert sig_event.signal_args == ["Research quantum computing"]

    @pytest.mark.asyncio
    async def test_sends_workflow_started(self, middleware, state_with_user_msg, runtime):
        await handle_before_agent(middleware, state_with_user_msg, runtime)
        calls = middleware._client.evaluate_event.call_args_list
        # Second call is WorkflowStarted
        wf_event = calls[1][0][0]
        assert wf_event.event_type == "WorkflowStarted"

    @pytest.mark.asyncio
    async def test_sends_prescreen_llm_started(self, middleware, state_with_user_msg, runtime):
        await handle_before_agent(middleware, state_with_user_msg, runtime)
        calls = middleware._client.evaluate_event.call_args_list
        # Third call is LLMStarted (pre-screen)
        llm_event = calls[2][0][0]
        assert llm_event.event_type == "LLMStarted"
        assert llm_event.prompt == "Research quantum computing"

    @pytest.mark.asyncio
    async def test_sets_workflow_and_run_ids(self, middleware, state_with_user_msg, runtime):
        await handle_before_agent(middleware, state_with_user_msg, runtime)
        assert middleware._workflow_id.startswith("test-thread-42-")
        assert middleware._run_id.startswith("test-thread-42-run-")
        assert middleware._thread_id == "test-thread-42"

    @pytest.mark.asyncio
    async def test_stores_prescreen_response(self, middleware, state_with_user_msg, runtime):
        await handle_before_agent(middleware, state_with_user_msg, runtime)
        assert middleware._pre_screen_response is not None
        assert middleware._pre_screen_response.verdict == Verdict.ALLOW

    @pytest.mark.asyncio
    async def test_skips_workflow_started_when_disabled(self, middleware, state_with_user_msg, runtime):
        middleware._config.send_chain_start_event = False
        await handle_before_agent(middleware, state_with_user_msg, runtime)
        calls = middleware._client.evaluate_event.call_args_list
        event_types = [c[0][0].event_type for c in calls]
        assert "WorkflowStarted" not in event_types

    @pytest.mark.asyncio
    async def test_block_verdict_raises_and_closes_workflow(self, middleware, state_with_user_msg, runtime):
        from openbox_langgraph.errors import GovernanceBlockedError
        middleware._client.evaluate_event = AsyncMock(side_effect=[
            GovernanceVerdictResponse(verdict=Verdict.ALLOW),  # SignalReceived
            GovernanceVerdictResponse(verdict=Verdict.ALLOW),  # WorkflowStarted
            GovernanceVerdictResponse(verdict=Verdict.BLOCK, reason="Blocked"),  # LLMStarted
            GovernanceVerdictResponse(verdict=Verdict.ALLOW),  # WorkflowCompleted(failed)
        ])
        with pytest.raises(GovernanceBlockedError):
            await handle_before_agent(middleware, state_with_user_msg, runtime)
        # Should have sent WorkflowCompleted(failed) to close the session
        calls = middleware._client.evaluate_event.call_args_list
        last_event = calls[-1][0][0]
        assert last_event.event_type == "WorkflowCompleted"
        assert last_event.status == "failed"


# ═══════════════════════════════════════════════════════════════════
# aafter_agent tests
# ═══════════════════════════════════════════════════════════════════

class TestAfterAgent:
    @pytest.mark.asyncio
    async def test_sends_workflow_completed(self, middleware, state_with_user_msg, runtime):
        middleware._workflow_id = "wf-123"
        middleware._run_id = "run-456"
        await handle_after_agent(middleware, state_with_user_msg, runtime)
        event = middleware._client.evaluate_event.call_args[0][0]
        assert event.event_type == "WorkflowCompleted"
        assert event.status == "completed"

    @pytest.mark.asyncio
    async def test_cleans_up_span_processor(self, middleware, state_with_user_msg, runtime):
        middleware._workflow_id = "wf-cleanup"
        await handle_after_agent(middleware, state_with_user_msg, runtime)
        middleware._span_processor.unregister_workflow.assert_called_once_with("wf-cleanup")

    @pytest.mark.asyncio
    async def test_skips_when_disabled(self, middleware, state_with_user_msg, runtime):
        middleware._config.send_chain_end_event = False
        middleware._workflow_id = "wf-skip"
        await handle_after_agent(middleware, state_with_user_msg, runtime)
        middleware._client.evaluate_event.assert_not_called()


# ═══════════════════════════════════════════════════════════════════
# awrap_model_call tests
# ═══════════════════════════════════════════════════════════════════

class TestWrapModelCall:
    @pytest.fixture
    def model_request(self):
        req = MagicMock()
        req.messages = [MagicMock(type="human", content="What is AI?")]
        req.model = MagicMock(__str__=lambda self: "gpt-4o-mini")
        return req

    @pytest.fixture
    def model_handler(self):
        response = MagicMock()
        response.message = MagicMock(
            content="AI is artificial intelligence.",
            response_metadata={"model_name": "gpt-4o-mini"},
            usage_metadata={"input_tokens": 10, "output_tokens": 20},
            tool_calls=[],
        )
        return AsyncMock(return_value=response)

    @pytest.mark.asyncio
    async def test_sends_llm_started_and_completed(self, middleware, model_request, model_handler):
        middleware._workflow_id = "wf-1"
        middleware._run_id = "run-1"
        middleware._first_llm_call = False
        middleware._pre_screen_response = None

        await handle_wrap_model_call(middleware, model_request, model_handler)

        calls = middleware._client.evaluate_event.call_args_list
        assert calls[0][0][0].event_type == "LLMStarted"
        assert calls[1][0][0].event_type == "LLMCompleted"
        assert calls[1][0][0].status == "completed"

    @pytest.mark.asyncio
    async def test_reuses_prescreen_response(self, middleware, model_request, model_handler):
        middleware._workflow_id = "wf-1"
        middleware._run_id = "run-1"
        middleware._first_llm_call = True
        middleware._pre_screen_response = GovernanceVerdictResponse(verdict=Verdict.ALLOW)

        await handle_wrap_model_call(middleware, model_request, model_handler)

        # First call should be LLMCompleted (not LLMStarted — reused pre_screen)
        calls = middleware._client.evaluate_event.call_args_list
        assert len(calls) == 1  # Only LLMCompleted
        assert calls[0][0][0].event_type == "LLMCompleted"

    @pytest.mark.asyncio
    async def test_skips_empty_prompt(self, middleware, model_handler):
        middleware._workflow_id = "wf-1"
        middleware._run_id = "run-1"
        req = MagicMock()
        req.messages = [MagicMock(type="system", content="You are a bot")]

        await handle_wrap_model_call(middleware, req, model_handler)
        model_handler.assert_called_once()
        middleware._client.evaluate_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_calls_handler(self, middleware, model_request, model_handler):
        middleware._workflow_id = "wf-1"
        middleware._run_id = "run-1"
        middleware._first_llm_call = False
        result = await handle_wrap_model_call(middleware, model_request, model_handler)
        model_handler.assert_called_once_with(model_request)
        assert result is model_handler.return_value

    @pytest.mark.asyncio
    async def test_extracts_token_metadata(self, middleware, model_request, model_handler):
        middleware._workflow_id = "wf-1"
        middleware._run_id = "run-1"
        middleware._first_llm_call = False
        await handle_wrap_model_call(middleware, model_request, model_handler)
        calls = middleware._client.evaluate_event.call_args_list
        completed = calls[1][0][0]
        assert completed.input_tokens == 10
        assert completed.output_tokens == 20
        assert completed.total_tokens == 30

    @pytest.mark.asyncio
    async def test_span_processor_lifecycle(self, middleware, model_request, model_handler):
        middleware._workflow_id = "wf-1"
        middleware._run_id = "run-1"
        middleware._first_llm_call = False
        await handle_wrap_model_call(middleware, model_request, model_handler)
        middleware._span_processor.set_activity_context.assert_called_once()
        middleware._span_processor.clear_activity_context.assert_called_once()


# ═══════════════════════════════════════════════════════════════════
# awrap_tool_call tests
# ═══════════════════════════════════════════════════════════════════

class TestWrapToolCall:
    @pytest.fixture
    def tool_request(self):
        req = MagicMock()
        req.tool_call = {"name": "search_web", "args": {"query": "quantum"}, "id": "call_1"}
        return req

    @pytest.fixture
    def task_request(self):
        req = MagicMock()
        req.tool_call = {"name": "task", "args": {"description": "Research AI", "subagent_type": "researcher"}, "id": "call_2"}
        return req

    @pytest.fixture
    def tool_handler(self):
        return AsyncMock(return_value=MagicMock(content="Search results..."))

    @pytest.mark.asyncio
    async def test_sends_tool_started_and_completed(self, middleware, tool_request, tool_handler):
        middleware._workflow_id = "wf-1"
        middleware._run_id = "run-1"
        await handle_wrap_tool_call(middleware, tool_request, tool_handler)

        calls = middleware._client.evaluate_event.call_args_list
        assert calls[0][0][0].event_type == "ToolStarted"
        assert calls[0][0][0].tool_name == "search_web"
        assert calls[0][0][0].tool_type == "http"
        assert calls[1][0][0].event_type == "ToolCompleted"

    @pytest.mark.asyncio
    async def test_subagent_detection(self, middleware, task_request, tool_handler):
        middleware._workflow_id = "wf-1"
        middleware._run_id = "run-1"
        await handle_wrap_tool_call(middleware, task_request, tool_handler)

        started = middleware._client.evaluate_event.call_args_list[0][0][0]
        assert started.subagent_name == "researcher"
        assert started.tool_type == "a2a"

    @pytest.mark.asyncio
    async def test_subagent_registers_span_processor(self, middleware, task_request, tool_handler):
        """Subagent tools get span-level governance — SpanProcessor context is registered."""
        middleware._workflow_id = "wf-1"
        middleware._run_id = "run-1"
        await handle_wrap_tool_call(middleware, task_request, tool_handler)
        middleware._span_processor.set_activity_context.assert_called_once()
        middleware._span_processor.clear_activity_context.assert_called_once()

    @pytest.mark.asyncio
    async def test_regular_tool_registers_span_processor(self, middleware, tool_request, tool_handler):
        middleware._workflow_id = "wf-1"
        middleware._run_id = "run-1"
        await handle_wrap_tool_call(middleware, tool_request, tool_handler)
        middleware._span_processor.set_activity_context.assert_called_once()
        middleware._span_processor.clear_activity_context.assert_called_once()

    @pytest.mark.asyncio
    async def test_tool_classification_metadata(self, middleware, tool_request, tool_handler):
        middleware._workflow_id = "wf-1"
        middleware._run_id = "run-1"
        await handle_wrap_tool_call(middleware, tool_request, tool_handler)

        started = middleware._client.evaluate_event.call_args_list[0][0][0]
        # activity_input should have __openbox sentinel
        has_sentinel = any(
            isinstance(item, dict) and "__openbox" in item
            for item in (started.activity_input or [])
        )
        assert has_sentinel

    @pytest.mark.asyncio
    async def test_skip_tool_types(self, middleware, tool_handler):
        middleware._workflow_id = "wf-1"
        middleware._run_id = "run-1"
        middleware._config.skip_tool_types = {"read_file"}
        req = MagicMock()
        req.tool_call = {"name": "read_file", "args": {"path": "/tmp"}, "id": "call_3"}

        await handle_wrap_tool_call(middleware, req, tool_handler)
        tool_handler.assert_called_once()
        middleware._client.evaluate_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_error_clears_span_processor(self, middleware, tool_request):
        middleware._workflow_id = "wf-1"
        middleware._run_id = "run-1"
        failing_handler = AsyncMock(side_effect=RuntimeError("tool failed"))

        with pytest.raises(RuntimeError, match="tool failed"):
            await handle_wrap_tool_call(middleware, tool_request, failing_handler)
        middleware._span_processor.clear_activity_context.assert_called_once()

    @pytest.mark.asyncio
    async def test_block_verdict_raises(self, middleware, tool_request, tool_handler):
        from openbox_langgraph.errors import GovernanceBlockedError
        middleware._workflow_id = "wf-1"
        middleware._run_id = "run-1"
        middleware._client.evaluate_event = AsyncMock(return_value=GovernanceVerdictResponse(
            verdict=Verdict.BLOCK, reason="Tool blocked",
        ))
        with pytest.raises(GovernanceBlockedError):
            await handle_wrap_tool_call(middleware, tool_request, tool_handler)
        tool_handler.assert_not_called()

    @pytest.mark.asyncio
    async def test_calls_handler(self, middleware, tool_request, tool_handler):
        middleware._workflow_id = "wf-1"
        middleware._run_id = "run-1"
        result = await handle_wrap_tool_call(middleware, tool_request, tool_handler)
        tool_handler.assert_called_once_with(tool_request)
        assert result is tool_handler.return_value


# ═══════════════════════════════════════════════════════════════════
# Factory tests
# ═══════════════════════════════════════════════════════════════════

class TestFactory:
    def test_create_openbox_middleware_returns_instance(self):
        with patch("openbox_langgraph.config.initialize"), \
             patch("openbox_deepagent.middleware.get_global_config") as mock_gc, \
             patch("openbox_deepagent.middleware.merge_config") as mock_mc:
            mock_gc.return_value = MagicMock(
                api_url="http://test", api_key="obx_test_key",
                governance_timeout=30.0,
            )
            mock_mc.return_value = MagicMock(
                on_api_error="fail_open", tool_type_map={},
                skip_tool_types=set(),
            )
            from openbox_deepagent.middleware_factory import create_openbox_middleware
            mw = create_openbox_middleware(
                api_url="http://test",
                api_key="obx_test_key",
                agent_name="TestBot",
                known_subagents=["researcher"],
            )
            assert isinstance(mw, OpenBoxMiddleware)
            assert mw.get_known_subagents() == ["researcher"]


# ═══════════════════════════════════════════════════════════════════
# OTel context propagation tests
# ═══════════════════════════════════════════════════════════════════

class TestOtelContextPropagation:
    """Verify OTel trace context bridges asyncio.Task boundaries."""

    @pytest.mark.asyncio
    async def test_run_with_otel_context_creates_span(self, middleware):
        """_run_with_otel_context creates an explicit child span."""
        middleware._workflow_id = "wf-otel"
        handler = AsyncMock(return_value="result")
        request = MagicMock()

        with patch("openbox_deepagent.middleware_hooks._tracer") as mock_tracer, \
             patch("openbox_deepagent.middleware_hooks.otel_context") as mock_ctx, \
             patch("openbox_deepagent.middleware_hooks.otel_trace") as mock_trace:
            mock_span = MagicMock()
            mock_span.get_span_context.return_value.trace_id = 12345
            mock_tracer.start_span.return_value = mock_span
            mock_ctx.get_current.return_value = "parent_ctx"
            mock_trace.set_span_in_context.return_value = "span_ctx"

            result = await _run_with_otel_context(
                middleware, "tool.search_web", "act-1", handler, request,
            )

            assert result == "result"
            mock_tracer.start_span.assert_called_once()
            call_kwargs = mock_tracer.start_span.call_args
            assert call_kwargs[0][0] == "tool.search_web"
            assert call_kwargs[1]["context"] == "parent_ctx"
            mock_span.end.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_with_otel_context_registers_trace(self, middleware):
        """Span's trace_id is registered with SpanProcessor."""
        middleware._workflow_id = "wf-otel"
        handler = AsyncMock(return_value="ok")

        with patch("openbox_deepagent.middleware_hooks._tracer") as mock_tracer, \
             patch("openbox_deepagent.middleware_hooks.otel_context"), \
             patch("openbox_deepagent.middleware_hooks.otel_trace"):
            mock_span = MagicMock()
            mock_span.get_span_context.return_value.trace_id = 99999
            mock_tracer.start_span.return_value = mock_span

            await _run_with_otel_context(
                middleware, "llm.call", "act-2", handler, MagicMock(),
            )

            middleware._span_processor.register_trace.assert_called_once_with(
                99999, "wf-otel", "act-2",
            )

    @pytest.mark.asyncio
    async def test_wrap_tool_call_uses_otel_span(self, middleware):
        """handle_wrap_tool_call delegates to _run_with_otel_context."""
        middleware._workflow_id = "wf-1"
        middleware._run_id = "run-1"
        tool_handler = AsyncMock(return_value=MagicMock(content="results"))
        tool_request = MagicMock()
        tool_request.tool_call = {"name": "search_web", "args": {"q": "test"}, "id": "c1"}

        with patch("openbox_deepagent.middleware_hooks._run_with_otel_context",
                    new_callable=AsyncMock, return_value=tool_handler.return_value) as mock_otel:
            await handle_wrap_tool_call(middleware, tool_request, tool_handler)
            mock_otel.assert_called_once()
            assert mock_otel.call_args[0][1].startswith("tool.")

    @pytest.mark.asyncio
    async def test_wrap_model_call_uses_otel_span(self, middleware):
        """handle_wrap_model_call delegates to _run_with_otel_context."""
        middleware._workflow_id = "wf-1"
        middleware._run_id = "run-1"
        middleware._first_llm_call = False
        middleware._pre_screen_response = None

        model_response = MagicMock()
        model_response.message = MagicMock(
            content="AI answer", response_metadata={"model_name": "gpt-4o"},
            usage_metadata={"input_tokens": 5, "output_tokens": 10}, tool_calls=[],
        )
        model_handler = AsyncMock(return_value=model_response)
        model_request = MagicMock()
        model_request.messages = [MagicMock(type="human", content="What is AI?")]
        model_request.model = MagicMock(__str__=lambda self: "gpt-4o")

        with patch("openbox_deepagent.middleware_hooks._run_with_otel_context",
                    new_callable=AsyncMock, return_value=model_response) as mock_otel:
            await handle_wrap_model_call(middleware, model_request, model_handler)
            mock_otel.assert_called_once()
            assert mock_otel.call_args[0][1] == "llm.call"


# ═══════════════════════════════════════════════════════════════════
# Hook-level HITL retry tests
# ═══════════════════════════════════════════════════════════════════

class TestHookHITLRetry:
    """Tests for REQUIRE_APPROVAL from OTel hooks (httpx/file/DB spans)."""

    @pytest.fixture
    def tool_request(self):
        req = MagicMock()
        req.tool_call = {"name": "search_web", "args": {"query": "test"}, "id": "call_1"}
        return req

    @pytest.mark.asyncio
    async def test_hook_require_approval_polls_and_retries(self, middleware, tool_request):
        """REQUIRE_APPROVAL from hook → poll → approval → retry tool."""
        from openbox_langgraph.errors import GovernanceBlockedError

        middleware._workflow_id = "wf-1"
        middleware._run_id = "run-1"
        success_result = MagicMock(content="Search results")

        # First call raises REQUIRE_APPROVAL, second succeeds
        call_count = 0

        async def mock_otel_context(mw, span_name, act_id, handler, request):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise GovernanceBlockedError("require_approval", "Needs approval", "https://api.com")
            return success_result

        with patch("openbox_deepagent.middleware_hooks._run_with_otel_context",
                    side_effect=mock_otel_context), \
             patch("openbox_deepagent.middleware_hooks.poll_until_decision",
                    new_callable=AsyncMock) as mock_poll:
            result = await handle_wrap_tool_call(middleware, tool_request, AsyncMock())

        assert result is success_result
        assert call_count == 2
        mock_poll.assert_called_once()
        middleware._span_processor.clear_activity_abort.assert_called_once()

    @pytest.mark.asyncio
    async def test_hook_require_approval_rejected(self, middleware, tool_request):
        """REQUIRE_APPROVAL → poll → rejected → GovernanceHaltError."""
        from openbox_langgraph.errors import GovernanceBlockedError, GovernanceHaltError
        from openbox_langgraph.errors import ApprovalRejectedError

        middleware._workflow_id = "wf-1"
        middleware._run_id = "run-1"

        async def mock_otel_context(mw, span_name, act_id, handler, request):
            raise GovernanceBlockedError("require_approval", "Needs approval", "https://api.com")

        with patch("openbox_deepagent.middleware_hooks._run_with_otel_context",
                    side_effect=mock_otel_context), \
             patch("openbox_deepagent.middleware_hooks.poll_until_decision",
                    new_callable=AsyncMock,
                    side_effect=ApprovalRejectedError("Rejected by reviewer")):
            with pytest.raises(GovernanceHaltError):
                await handle_wrap_tool_call(middleware, tool_request, AsyncMock())

        middleware._span_processor.clear_activity_context.assert_called()

    @pytest.mark.asyncio
    async def test_hook_require_approval_timeout(self, middleware, tool_request):
        """REQUIRE_APPROVAL → poll → timeout → GovernanceHaltError."""
        from openbox_langgraph.errors import GovernanceBlockedError, GovernanceHaltError
        from openbox_langgraph.errors import ApprovalTimeoutError

        middleware._workflow_id = "wf-1"
        middleware._run_id = "run-1"

        async def mock_otel_context(mw, span_name, act_id, handler, request):
            raise GovernanceBlockedError("require_approval", "Needs approval", "https://api.com")

        with patch("openbox_deepagent.middleware_hooks._run_with_otel_context",
                    side_effect=mock_otel_context), \
             patch("openbox_deepagent.middleware_hooks.poll_until_decision",
                    new_callable=AsyncMock,
                    side_effect=ApprovalTimeoutError(600000)):
            with pytest.raises(GovernanceHaltError):
                await handle_wrap_tool_call(middleware, tool_request, AsyncMock())

    @pytest.mark.asyncio
    async def test_hook_block_verdict_still_propagates(self, middleware, tool_request):
        """BLOCK from hook → propagates as GovernanceBlockedError, not caught by retry."""
        from openbox_langgraph.errors import GovernanceBlockedError

        middleware._workflow_id = "wf-1"
        middleware._run_id = "run-1"

        async def mock_otel_context(mw, span_name, act_id, handler, request):
            raise GovernanceBlockedError("block", "Blocked by policy", "https://api.com")

        with patch("openbox_deepagent.middleware_hooks._run_with_otel_context",
                    side_effect=mock_otel_context):
            with pytest.raises(GovernanceBlockedError):
                await handle_wrap_tool_call(middleware, tool_request, AsyncMock())

        # Should have sent ToolCompleted(failed)
        calls = middleware._client.evaluate_event.call_args_list
        event_types = [c[0][0].event_type for c in calls]
        assert "ToolCompleted" in event_types

    @pytest.mark.asyncio
    async def test_hook_require_approval_clears_abort_flag(self, middleware, tool_request):
        """Abort flag cleared before retry so subsequent hooks don't short-circuit."""
        from openbox_langgraph.errors import GovernanceBlockedError

        middleware._workflow_id = "wf-1"
        middleware._run_id = "run-1"

        call_count = 0

        async def mock_otel_context(mw, span_name, act_id, handler, request):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise GovernanceBlockedError("require_approval", "Approval needed", "file:///tmp/x")
            return MagicMock(content="ok")

        with patch("openbox_deepagent.middleware_hooks._run_with_otel_context",
                    side_effect=mock_otel_context), \
             patch("openbox_deepagent.middleware_hooks.poll_until_decision",
                    new_callable=AsyncMock):
            await handle_wrap_tool_call(middleware, tool_request, AsyncMock())

        # clear_activity_abort called with workflow_id and the activity_id
        middleware._span_processor.clear_activity_abort.assert_called_once()
        args = middleware._span_processor.clear_activity_abort.call_args[0]
        assert args[0] == "wf-1"  # workflow_id

    @pytest.mark.asyncio
    async def test_wrapped_require_approval_polls_and_retries(self, middleware):
        """Wrapped GovernanceBlockedError (e.g. subagent LLM → OpenAI SDK) → poll → retry."""
        from openbox_langgraph.errors import GovernanceBlockedError

        middleware._workflow_id = "wf-1"
        middleware._run_id = "run-1"
        success_result = MagicMock(content="Task completed")

        call_count = 0
        req = MagicMock()
        req.tool_call = {"name": "task", "args": {"description": "Research AI", "subagent_type": "researcher"}, "id": "c1"}

        async def mock_otel_context(mw, span_name, act_id, handler, request):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                gov_err = GovernanceBlockedError("require_approval", "Approval needed", "https://api.openai.com")
                raise RuntimeError("Connection error.") from gov_err
            return success_result

        with patch("openbox_deepagent.middleware_hooks._run_with_otel_context",
                    side_effect=mock_otel_context), \
             patch("openbox_deepagent.middleware_hooks.poll_until_decision",
                    new_callable=AsyncMock) as mock_poll:
            result = await handle_wrap_tool_call(middleware, req, AsyncMock())

        assert result is success_result
        assert call_count == 2
        mock_poll.assert_called_once()


# ═══════════════════════════════════════════════════════════════════
# Hook-level HITL retry in model call tests
# ═══════════════════════════════════════════════════════════════════

class TestModelCallHookHITLRetry:
    """Tests for REQUIRE_APPROVAL from OTel hooks during LLM calls."""

    @pytest.fixture
    def model_request(self):
        req = MagicMock()
        req.messages = [MagicMock(type="human", content="What is AI?")]
        req.model = MagicMock(__str__=lambda self: "gpt-4o-mini")
        return req

    @pytest.fixture
    def model_response(self):
        resp = MagicMock()
        resp.message = MagicMock(
            content="AI is artificial intelligence.",
            response_metadata={"model_name": "gpt-4o-mini"},
            usage_metadata={"input_tokens": 10, "output_tokens": 20},
            tool_calls=[],
        )
        return resp

    @pytest.mark.asyncio
    async def test_direct_require_approval_polls_and_retries(self, middleware, model_request, model_response):
        """Direct GovernanceBlockedError(require_approval) → poll → retry LLM call."""
        from openbox_langgraph.errors import GovernanceBlockedError

        middleware._workflow_id = "wf-1"
        middleware._run_id = "run-1"
        middleware._first_llm_call = False
        middleware._pre_screen_response = None

        call_count = 0

        async def mock_otel_context(mw, span_name, act_id, handler, request):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise GovernanceBlockedError("require_approval", "Needs approval", "https://api.openai.com")
            return model_response

        with patch("openbox_deepagent.middleware_hooks._run_with_otel_context",
                    side_effect=mock_otel_context), \
             patch("openbox_deepagent.middleware_hooks.poll_until_decision",
                    new_callable=AsyncMock):
            result = await handle_wrap_model_call(middleware, model_request, AsyncMock())

        assert call_count == 2
        assert result is model_response

    @pytest.mark.asyncio
    async def test_wrapped_require_approval_polls_and_retries(self, middleware, model_request, model_response):
        """Wrapped GovernanceBlockedError (e.g. inside APIConnectionError) → poll → retry."""
        from openbox_langgraph.errors import GovernanceBlockedError

        middleware._workflow_id = "wf-1"
        middleware._run_id = "run-1"
        middleware._first_llm_call = False
        middleware._pre_screen_response = None

        call_count = 0

        async def mock_otel_context(mw, span_name, act_id, handler, request):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Simulate OpenAI SDK wrapping: raise SomeError from GovernanceBlockedError
                gov_err = GovernanceBlockedError("require_approval", "Approval needed", "https://api.openai.com")
                raise RuntimeError("Connection error.") from gov_err
            return model_response

        with patch("openbox_deepagent.middleware_hooks._run_with_otel_context",
                    side_effect=mock_otel_context), \
             patch("openbox_deepagent.middleware_hooks.poll_until_decision",
                    new_callable=AsyncMock):
            result = await handle_wrap_model_call(middleware, model_request, AsyncMock())

        assert call_count == 2
        assert result is model_response

    @pytest.mark.asyncio
    async def test_wrapped_non_governance_error_propagates(self, middleware, model_request):
        """Non-governance error wrapped → propagates as-is."""
        middleware._workflow_id = "wf-1"
        middleware._run_id = "run-1"
        middleware._first_llm_call = False
        middleware._pre_screen_response = None

        async def mock_otel_context(mw, span_name, act_id, handler, request):
            raise RuntimeError("Actual connection error")

        with patch("openbox_deepagent.middleware_hooks._run_with_otel_context",
                    side_effect=mock_otel_context):
            with pytest.raises(RuntimeError, match="Actual connection error"):
                await handle_wrap_model_call(middleware, model_request, AsyncMock())

    @pytest.mark.asyncio
    async def test_wrapped_require_approval_rejected(self, middleware, model_request):
        """Wrapped REQUIRE_APPROVAL → poll → rejected → GovernanceHaltError."""
        from openbox_langgraph.errors import GovernanceBlockedError, GovernanceHaltError
        from openbox_langgraph.errors import ApprovalRejectedError

        middleware._workflow_id = "wf-1"
        middleware._run_id = "run-1"
        middleware._first_llm_call = False
        middleware._pre_screen_response = None

        async def mock_otel_context(mw, span_name, act_id, handler, request):
            gov_err = GovernanceBlockedError("require_approval", "Needs approval", "https://api.openai.com")
            raise RuntimeError("Connection error.") from gov_err

        with patch("openbox_deepagent.middleware_hooks._run_with_otel_context",
                    side_effect=mock_otel_context), \
             patch("openbox_deepagent.middleware_hooks.poll_until_decision",
                    new_callable=AsyncMock,
                    side_effect=ApprovalRejectedError("Rejected")):
            with pytest.raises(GovernanceHaltError):
                await handle_wrap_model_call(middleware, model_request, AsyncMock())
