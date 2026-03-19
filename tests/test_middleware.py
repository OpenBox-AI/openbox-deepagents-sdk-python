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
