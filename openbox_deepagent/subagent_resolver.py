"""Shared DeepAgents utilities for subagent detection and conflict guards.

Used by both the legacy OpenBoxDeepAgentHandler and the new OpenBoxMiddleware.
"""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from openbox_langgraph.types import LangGraphStreamEvent


# ═══════════════════════════════════════════════════════════════════
# DeepAgents built-in tool names
# ═══════════════════════════════════════════════════════════════════

DEEPAGENT_BUILTIN_TOOLS: frozenset[str] = frozenset({
    "write_todos",
    "ls",
    "read_file",
    "write_file",
    "edit_file",
    "glob",
    "grep",
    "execute",
    "task",
})

DEEPAGENT_SUBAGENT_TOOL = "task"


# ═══════════════════════════════════════════════════════════════════
# Subagent name resolver (from LangGraph stream events)
# ═══════════════════════════════════════════════════════════════════

def resolve_deepagent_subagent_name(event: LangGraphStreamEvent) -> str | None:
    """Detect DeepAgents subagent invocations from the `task` tool's `on_tool_start` event.

    DeepAgents subagents run synchronously inside the `task` tool body — their
    events are NOT visible in the outer stream. The only observable signal is
    the `task` tool's `on_tool_start` event, which carries `subagent_type` in
    its input dict.

    Returns the `subagent_type` string (e.g. `"weather"`, `"general-purpose"`),
    or None for all other events.
    """
    if event.event != "on_tool_start":
        return None
    if event.name != DEEPAGENT_SUBAGENT_TOOL:
        return None

    raw_input = event.data.get("input")
    if isinstance(raw_input, dict):
        subagent_type = raw_input.get("subagent_type")
        if isinstance(subagent_type, str):
            return subagent_type

    # Fallback: DeepAgents default is general-purpose
    if sys.stderr and os.environ.get("OPENBOX_DEBUG"):
        sys.stderr.write(
            f"[OpenBox Debug] task tool input missing subagent_type, "
            f"defaulting to general-purpose. raw_input={raw_input!r}\n"
        )
    return "general-purpose"


# ═══════════════════════════════════════════════════════════════════
# Subagent name resolver (from tool_call dict — used by middleware)
# ═══════════════════════════════════════════════════════════════════

def resolve_subagent_from_tool_call(tool_name: str, tool_args: Any) -> str | None:
    """Extract subagent_type from a tool_call dict (middleware hook context).

    Unlike resolve_deepagent_subagent_name which works on LangGraph stream events,
    this works on the raw tool_call data available in wrap_tool_call hooks.

    Returns the subagent_type string, or None if not a subagent tool.
    """
    if tool_name != DEEPAGENT_SUBAGENT_TOOL:
        return None

    if isinstance(tool_args, dict):
        subagent_type = tool_args.get("subagent_type")
        if isinstance(subagent_type, str):
            return subagent_type

    # Fallback: DeepAgents default is general-purpose
    if sys.stderr and os.environ.get("OPENBOX_DEBUG"):
        sys.stderr.write(
            f"[OpenBox Debug] task tool_call missing subagent_type, "
            f"defaulting to general-purpose. tool_args={tool_args!r}\n"
        )
    return "general-purpose"


# ═══════════════════════════════════════════════════════════════════
# HITL / interrupt_on conflict detection
# ═══════════════════════════════════════════════════════════════════

def hitl_enabled(hitl: Any) -> bool:
    """Return True if the HITL config indicates polling is enabled."""
    if hitl is None:
        return False
    if isinstance(hitl, dict):
        return bool(hitl.get("enabled", False))
    return bool(getattr(hitl, "enabled", False))


def graph_has_interrupt_on(graph: Any) -> bool:
    """Check whether the compiled graph has interrupt_before or interrupt_after configured.

    DeepAgents HumanInTheLoopMiddleware sets these on the compiled graph.
    This is a best-effort check — false negatives are possible for custom setups.
    """
    interrupt_before = getattr(graph, "interrupt_before", None) or getattr(graph, "interruptBefore", None)
    interrupt_after = getattr(graph, "interrupt_after", None) or getattr(graph, "interruptAfter", None)
    if isinstance(interrupt_before, (list, tuple, set)) and len(interrupt_before) > 0:
        return True
    if isinstance(interrupt_after, (list, tuple, set)) and len(interrupt_after) > 0:
        return True
    return False
