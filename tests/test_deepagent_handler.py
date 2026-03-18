"""Tests for openbox_deepagent.deepagent_handler.

Covers:
- _resolve_deepagent_subagent_name: all input shapes
- _hitl_enabled: dict and dataclass inputs
- _graph_has_interrupt_on: interrupt_before / interrupt_after detection
- OpenBoxDeepAgentHandlerOptions: field inheritance via dataclasses.fields()
- create_openbox_deep_agent_handler: kwargs forwarding, conflict guard, validation
- OpenBoxDeepAgentHandler: known_subagents warning, subagent resolver injection
"""

from __future__ import annotations

import dataclasses
import warnings
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from openbox_deepagent.deepagent_handler import (
    DEEPAGENT_SUBAGENT_TOOL,
    OpenBoxDeepAgentHandler,
    OpenBoxDeepAgentHandlerOptions,
    _graph_has_interrupt_on,
    _hitl_enabled,
    _resolve_deepagent_subagent_name,
)
from openbox_langgraph.types import HITLConfig, LangGraphStreamEvent


# ═══════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════

def _make_event(
    event: str,
    name: str,
    data: dict[str, Any] | None = None,
) -> LangGraphStreamEvent:
    return LangGraphStreamEvent.from_dict({
        "event": event,
        "name": name,
        "run_id": "run-test",
        "data": data or {},
        "metadata": {},
        "tags": [],
    })


def _make_mock_graph(
    interrupt_before: list[str] | None = None,
    interrupt_after: list[str] | None = None,
) -> MagicMock:
    graph = MagicMock()
    graph.interrupt_before = interrupt_before or []
    graph.interrupt_after = interrupt_after or []
    return graph


# ═══════════════════════════════════════════════════════════════════
# _resolve_deepagent_subagent_name
# ═══════════════════════════════════════════════════════════════════

class TestResolveDeepagentSubagentName:
    def test_non_tool_start_event_returns_none(self) -> None:
        event = _make_event("on_tool_end", DEEPAGENT_SUBAGENT_TOOL)
        assert _resolve_deepagent_subagent_name(event) is None

    def test_non_task_tool_returns_none(self) -> None:
        event = _make_event("on_tool_start", "search_web")
        assert _resolve_deepagent_subagent_name(event) is None

    def test_task_tool_with_subagent_type_returns_it(self) -> None:
        event = _make_event(
            "on_tool_start",
            DEEPAGENT_SUBAGENT_TOOL,
            data={"input": {"subagent_type": "researcher"}},
        )
        assert _resolve_deepagent_subagent_name(event) == "researcher"

    def test_task_tool_with_general_purpose_subagent(self) -> None:
        event = _make_event(
            "on_tool_start",
            DEEPAGENT_SUBAGENT_TOOL,
            data={"input": {"subagent_type": "general-purpose"}},
        )
        assert _resolve_deepagent_subagent_name(event) == "general-purpose"

    def test_task_tool_missing_subagent_type_falls_back(self) -> None:
        event = _make_event(
            "on_tool_start",
            DEEPAGENT_SUBAGENT_TOOL,
            data={"input": {"description": "do something"}},
        )
        assert _resolve_deepagent_subagent_name(event) == "general-purpose"

    def test_task_tool_non_dict_input_falls_back(self) -> None:
        event = _make_event(
            "on_tool_start",
            DEEPAGENT_SUBAGENT_TOOL,
            data={"input": "string input"},
        )
        assert _resolve_deepagent_subagent_name(event) == "general-purpose"

    def test_task_tool_no_input_key_falls_back(self) -> None:
        event = _make_event("on_tool_start", DEEPAGENT_SUBAGENT_TOOL, data={})
        assert _resolve_deepagent_subagent_name(event) == "general-purpose"

    def test_task_tool_subagent_type_non_string_falls_back(self) -> None:
        event = _make_event(
            "on_tool_start",
            DEEPAGENT_SUBAGENT_TOOL,
            data={"input": {"subagent_type": 42}},
        )
        assert _resolve_deepagent_subagent_name(event) == "general-purpose"

    def test_fallback_debug_log(self, capsys: pytest.CaptureFixture[str]) -> None:
        event = _make_event(
            "on_tool_start",
            DEEPAGENT_SUBAGENT_TOOL,
            data={"input": {}},
        )
        with patch.dict("os.environ", {"OPENBOX_DEBUG": "1"}):
            result = _resolve_deepagent_subagent_name(event)
        assert result == "general-purpose"
        captured = capsys.readouterr()
        assert "general-purpose" in captured.err
        assert "subagent_type" in captured.err


# ═══════════════════════════════════════════════════════════════════
# _hitl_enabled
# ═══════════════════════════════════════════════════════════════════

class TestHitlEnabled:
    def test_none_returns_false(self) -> None:
        assert _hitl_enabled(None) is False

    def test_dict_enabled_true(self) -> None:
        assert _hitl_enabled({"enabled": True}) is True

    def test_dict_enabled_false(self) -> None:
        assert _hitl_enabled({"enabled": False}) is False

    def test_dict_missing_enabled_key(self) -> None:
        assert _hitl_enabled({"poll_interval_ms": 5000}) is False

    def test_hitlconfig_enabled(self) -> None:
        cfg = HITLConfig(enabled=True)
        assert _hitl_enabled(cfg) is True

    def test_hitlconfig_disabled(self) -> None:
        cfg = HITLConfig(enabled=False)
        assert _hitl_enabled(cfg) is False

    def test_arbitrary_object_with_enabled(self) -> None:
        obj = MagicMock()
        obj.enabled = True
        assert _hitl_enabled(obj) is True


# ═══════════════════════════════════════════════════════════════════
# _graph_has_interrupt_on
# ═══════════════════════════════════════════════════════════════════

class TestGraphHasInterruptOn:
    def test_empty_interrupt_lists_returns_false(self) -> None:
        graph = _make_mock_graph(interrupt_before=[], interrupt_after=[])
        assert _graph_has_interrupt_on(graph) is False

    def test_interrupt_before_set_returns_true(self) -> None:
        graph = _make_mock_graph(interrupt_before=["tools"])
        assert _graph_has_interrupt_on(graph) is True

    def test_interrupt_after_set_returns_true(self) -> None:
        graph = _make_mock_graph(interrupt_after=["agent"])
        assert _graph_has_interrupt_on(graph) is True

    def test_both_set_returns_true(self) -> None:
        graph = _make_mock_graph(interrupt_before=["a"], interrupt_after=["b"])
        assert _graph_has_interrupt_on(graph) is True

    def test_no_interrupt_attrs_returns_false(self) -> None:
        graph = MagicMock(spec=[])
        assert _graph_has_interrupt_on(graph) is False

    def test_camel_case_interrupt_before(self) -> None:
        graph = MagicMock(spec=[])
        graph.interruptBefore = ["tools"]
        graph.interruptAfter = []
        assert _graph_has_interrupt_on(graph) is True


# ═══════════════════════════════════════════════════════════════════
# OpenBoxDeepAgentHandlerOptions — field inheritance
# ═══════════════════════════════════════════════════════════════════

class TestDeepAgentHandlerOptions:
    def test_inherits_all_parent_fields(self) -> None:
        all_field_names = {f.name for f in dataclasses.fields(OpenBoxDeepAgentHandlerOptions)}
        # Parent fields that must be present
        for parent_field in ("agent_name", "hitl", "session_id", "on_api_error",
                             "skip_chain_types", "skip_tool_types", "tool_type_map"):
            assert parent_field in all_field_names, f"Parent field '{parent_field}' missing"

    def test_deepagent_own_fields_present(self) -> None:
        all_field_names = {f.name for f in dataclasses.fields(OpenBoxDeepAgentHandlerOptions)}
        assert "known_subagents" in all_field_names
        assert "guard_interrupt_on_conflict" in all_field_names

    def test_defaults(self) -> None:
        opts = OpenBoxDeepAgentHandlerOptions()
        assert opts.known_subagents == ["general-purpose"]
        assert opts.guard_interrupt_on_conflict is True


# ═══════════════════════════════════════════════════════════════════
# OpenBoxDeepAgentHandler — construction
# ═══════════════════════════════════════════════════════════════════

class TestOpenBoxDeepAgentHandler:
    def _make_handler(self, **opts_kwargs: Any) -> OpenBoxDeepAgentHandler:
        graph = _make_mock_graph()
        opts = OpenBoxDeepAgentHandlerOptions(**opts_kwargs)
        with patch("openbox_langgraph.langgraph_handler.GovernanceClient"):
            return OpenBoxDeepAgentHandler(graph, opts)

    def test_get_known_subagents_sorted(self) -> None:
        handler = self._make_handler(known_subagents=["writer", "analyst", "researcher"])
        assert handler.get_known_subagents() == ["analyst", "researcher", "writer"]

    def test_get_known_subagents_default(self) -> None:
        handler = self._make_handler()
        assert handler.get_known_subagents() == ["general-purpose"]

    def test_empty_known_subagents_warns(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            self._make_handler(known_subagents=[])
        messages = [str(w.message) for w in caught]
        assert any("known_subagents is empty" in m for m in messages)

    def test_interrupt_conflict_raises(self) -> None:
        graph = _make_mock_graph(interrupt_before=["tools"])
        opts = OpenBoxDeepAgentHandlerOptions(hitl={"enabled": True}, guard_interrupt_on_conflict=True)
        with patch("openbox_langgraph.langgraph_handler.GovernanceClient"):
            with pytest.raises(ValueError, match="interrupt_on"):
                OpenBoxDeepAgentHandler(graph, opts)

    def test_interrupt_conflict_disabled_does_not_raise(self) -> None:
        handler = self._make_handler(
            hitl={"enabled": True},
            guard_interrupt_on_conflict=False,
        )
        assert handler is not None

    def test_resolver_injected(self) -> None:
        handler = self._make_handler()
        assert handler._resolve_subagent_name is _resolve_deepagent_subagent_name


# ═══════════════════════════════════════════════════════════════════
# create_openbox_deep_agent_handler — factory
# ═══════════════════════════════════════════════════════════════════

class TestCreateOpenboxDeepAgentHandler:
    def _call_factory(self, **kwargs: Any) -> OpenBoxDeepAgentHandler:
        graph = _make_mock_graph()
        with patch("openbox_langgraph.config.initialize"):
            with patch("openbox_langgraph.langgraph_handler.GovernanceClient"):
                from openbox_deepagent.deepagent_handler import create_openbox_deep_agent_handler
                return create_openbox_deep_agent_handler(
                    graph=graph,
                    api_url="https://core.openbox.ai",
                    api_key="obx_live_testkey",
                    validate=False,
                    **kwargs,
                )

    def test_returns_handler_instance(self) -> None:
        handler = self._call_factory()
        assert isinstance(handler, OpenBoxDeepAgentHandler)

    def test_parent_kwargs_forwarded(self) -> None:
        handler = self._call_factory(agent_name="TestBot", session_id="sess-123")
        assert handler._config.agent_name == "TestBot"
        assert handler._config.session_id == "sess-123"

    def test_hitl_kwargs_forwarded(self) -> None:
        handler = self._call_factory(hitl={"enabled": True, "poll_interval_ms": 3000})
        assert handler._config.hitl.enabled is True
        assert handler._config.hitl.poll_interval_ms == 3000

    def test_skip_chain_types_forwarded(self) -> None:
        handler = self._call_factory(skip_chain_types={"model", "tools"})
        assert "model" in handler._config.skip_chain_types

    def test_tool_type_map_forwarded(self) -> None:
        handler = self._call_factory(tool_type_map={"search_web": "http"})
        assert handler._config.tool_type_map == {"search_web": "http"}

    def test_unknown_kwargs_ignored_silently(self) -> None:
        handler = self._call_factory(nonexistent_option="value")
        assert handler is not None

    def test_known_subagents_forwarded(self) -> None:
        handler = self._call_factory(known_subagents=["researcher", "analyst"])
        assert set(handler.get_known_subagents()) == {"researcher", "analyst"}

    def test_default_known_subagents(self) -> None:
        handler = self._call_factory()
        assert handler.get_known_subagents() == ["general-purpose"]

    def test_initialize_called(self) -> None:
        with patch("openbox_langgraph.config.initialize") as mock_init:
            with patch("openbox_langgraph.langgraph_handler.GovernanceClient"):
                from openbox_deepagent.deepagent_handler import create_openbox_deep_agent_handler
                create_openbox_deep_agent_handler(
                    graph=_make_mock_graph(),
                    api_url="https://core.openbox.ai",
                    api_key="obx_live_testkey",
                    validate=False,
                )
        mock_init.assert_called_once_with(
            api_url="https://core.openbox.ai",
            api_key="obx_live_testkey",
            governance_timeout=30.0,
            validate=False,
        )
