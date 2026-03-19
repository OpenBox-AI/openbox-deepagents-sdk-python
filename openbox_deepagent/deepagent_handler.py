"""OpenBox DeepAgents SDK — OpenBoxDeepAgentHandler.

Extends OpenBoxLangGraphHandler with DeepAgents-specific governance.

Architecture note — subagents are invisible to the outer stream
================================================================
In DeepAgents, subagents are invoked via the built-in `task` tool:

    task(description="...", subagent_type="weather")

The `task` tool calls `subagent.invoke()` synchronously inside its body.
This means subagent events do NOT appear in the outer LangGraph event stream —
they are invisible. Only the `task` tool itself appears as
`on_tool_start` / `on_tool_end` in the outer stream.

What this handler adds on top of openbox-langgraph-sdk
=======================================================
1. Task tool subagent detection: reads `subagent_type` from `on_tool_start`
   for the `task` tool and surfaces it as `subagent_name` on the governance
   event, so OpenBox Core Rego policies can target specific subagent types.

2. interrupt_on conflict guard: raises at construction time if the graph has
   `interrupt_before`/`interrupt_after` set (HumanInTheLoopMiddleware) AND
   OpenBox HITL is enabled, preventing double-HITL confusion.

3. Known subagent registry: stores the list of configured subagents for
   enrichment and introspection.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from openbox_langgraph.langgraph_handler import (
    OpenBoxLangGraphHandler,
    OpenBoxLangGraphHandlerOptions,
    create_openbox_graph_handler,
)

# Shared utilities — used by both this handler and the new OpenBoxMiddleware
from openbox_deepagent.subagent_resolver import (
    DEEPAGENT_BUILTIN_TOOLS,
    DEEPAGENT_SUBAGENT_TOOL,
    graph_has_interrupt_on as _graph_has_interrupt_on,
    hitl_enabled as _hitl_enabled,
    resolve_deepagent_subagent_name as _resolve_deepagent_subagent_name,
)

if TYPE_CHECKING:
    from langgraph.graph.state import CompiledStateGraph


# ═══════════════════════════════════════════════════════════════════
# Options
# ═══════════════════════════════════════════════════════════════════

@dataclass
class OpenBoxDeepAgentHandlerOptions(OpenBoxLangGraphHandlerOptions):
    """Options for `OpenBoxDeepAgentHandler`.

    Extends `OpenBoxLangGraphHandlerOptions` with DeepAgents-specific fields.
    """

    known_subagents: list[str] = field(default_factory=lambda: ["general-purpose"])
    """Subagent names configured in `create_deep_agent(subagents=[...])`.

    Used to enrich governance events with known subagent types.
    Include `"general-purpose"` if the default general-purpose subagent is active.
    Example: `["general-purpose", "researcher", "coder"]`
    """

    guard_interrupt_on_conflict: bool = True
    """If True, raise at construction time when HITL is enabled and the graph
    has `interrupt_on` configured (HumanInTheLoopMiddleware).

    This prevents double-HITL where both DeepAgents and OpenBox try to pause
    execution for human approval.
    """


# ═══════════════════════════════════════════════════════════════════
# OpenBoxDeepAgentHandler
# ═══════════════════════════════════════════════════════════════════

class OpenBoxDeepAgentHandler(OpenBoxLangGraphHandler):
    """Wraps a DeepAgents graph with OpenBox governance.

    Extends `OpenBoxLangGraphHandler` by:
    - Plugging in DeepAgents-specific subagent detection (`task` tool interception)
    - Guarding against `interrupt_on` / OpenBox HITL conflicts
    - Exposing the known subagent registry

    Usage:
        from deepagents.graph import create_deep_agent
        from openbox_deepagent import create_openbox_deep_agent_handler

        # IMPORTANT: do NOT pass interrupt_on to create_deep_agent when using OpenBox HITL
        agent = create_deep_agent(model="claude-sonnet-4-5", tools=[my_tool])

        governed = await create_openbox_deep_agent_handler(
            graph=agent,
            api_url=os.environ["OPENBOX_URL"],
            api_key=os.environ["OPENBOX_API_KEY"],
            agent_name="MyDeepAgent",
            known_subagents=["general-purpose", "researcher"],
            hitl={"enabled": True, "poll_interval_ms": 5000, "max_wait_ms": 300000},
        )

        result = await governed.ainvoke(
            {"messages": [{"role": "user", "content": "Research LangGraph"}]},
            config={"configurable": {"thread_id": "session-abc"}},
        )
    """

    def __init__(
        self,
        graph: CompiledStateGraph,
        options: OpenBoxDeepAgentHandlerOptions | None = None,
    ) -> None:
        opts = options or OpenBoxDeepAgentHandlerOptions()

        if not opts.known_subagents:
            import warnings
            warnings.warn(
                "[OpenBox] known_subagents is empty — subagent policy targeting disabled",
                stacklevel=2,
            )

        # Inject the DeepAgents subagent resolver
        opts.resolve_subagent_name = _resolve_deepagent_subagent_name

        super().__init__(graph, opts)

        self._known_subagents: frozenset[str] = frozenset(opts.known_subagents)

        # Guard: raise if interrupt_on conflict is detected
        if (
            opts.hitl
            and _hitl_enabled(opts.hitl)
            and opts.guard_interrupt_on_conflict
            and _graph_has_interrupt_on(graph)
        ):
            msg = (
                "[OpenBox] DeepAgents graph has interrupt_on (HumanInTheLoopMiddleware) configured "
                "AND OpenBox HITL is enabled. These conflict — OpenBox must own the HITL flow. "
                "Remove interrupt_on from create_deep_agent, "
                "or set guard_interrupt_on_conflict=False to skip this check."
            )
            raise ValueError(msg)

    def get_known_subagents(self) -> list[str]:
        """Return the known subagent names registered with this handler."""
        return sorted(self._known_subagents)


# ═══════════════════════════════════════════════════════════════════
# Factory
# ═══════════════════════════════════════════════════════════════════

def create_openbox_deep_agent_handler(
    graph: CompiledStateGraph,
    *,
    api_url: str,
    api_key: str,
    governance_timeout: float = 30.0,
    validate: bool = True,
    known_subagents: list[str] | None = None,
    guard_interrupt_on_conflict: bool = True,
    **handler_kwargs: Any,
) -> OpenBoxDeepAgentHandler:
    """Create a fully configured `OpenBoxDeepAgentHandler` for a DeepAgents graph.

    .. deprecated::
        Use :func:`create_openbox_middleware` with ``create_deep_agent(middleware=[...])`` instead.

    Validates the API key and sets up global config before returning the handler.

    IMPORTANT: Do NOT pass `interrupt_on` to `create_deep_agent` when using OpenBox
    HITL. DeepAgents' `interrupt_on` (HumanInTheLoopMiddleware) conflicts with
    OpenBox's polling-based approval. If both are active, this factory raises a
    `ValueError` to prevent double-HITL.

    Args:
        graph: A compiled LangGraph graph returned by `create_deep_agent()`.
        api_url: Base URL of your OpenBox Core instance.
        api_key: API key in `obx_live_*` or `obx_test_*` format.
        governance_timeout: HTTP timeout in **seconds** for governance calls (default 30.0).
        validate: If True, validates the API key against the server on startup.
        known_subagents: Subagent names from `create_deep_agent(subagents=[...])`.
            Defaults to `["general-purpose"]`.
        guard_interrupt_on_conflict: If True, raise when `interrupt_on` and OpenBox
            HITL are both enabled.
        **handler_kwargs: Additional keyword arguments forwarded to
            `OpenBoxDeepAgentHandlerOptions` (e.g. `agent_name`, `hitl`, `session_id`).

    Returns:
        A configured `OpenBoxDeepAgentHandler` ready to govern the DeepAgents graph.

    Example:
        >>> governed = create_openbox_deep_agent_handler(
        ...     graph=agent,
        ...     api_url=os.environ["OPENBOX_URL"],
        ...     api_key=os.environ["OPENBOX_API_KEY"],
        ...     agent_name="MyDeepAgent",
        ...     known_subagents=["general-purpose", "researcher"],
        ...     hitl={"enabled": True, "poll_interval_ms": 5000, "max_wait_ms": 300000},
        ... )
    """
    import warnings
    warnings.warn(
        "create_openbox_deep_agent_handler is deprecated. "
        "Use create_openbox_middleware() with create_deep_agent(middleware=[...]) instead.",
        DeprecationWarning,
        stacklevel=2,
    )

    from openbox_langgraph.config import initialize
    initialize(
        api_url=api_url,
        api_key=api_key,
        governance_timeout=governance_timeout,
        validate=validate,
    )

    valid_fields = {f.name for f in dataclasses.fields(OpenBoxDeepAgentHandlerOptions)}
    options = OpenBoxDeepAgentHandlerOptions(
        api_timeout=governance_timeout,
        known_subagents=known_subagents or ["general-purpose"],
        guard_interrupt_on_conflict=guard_interrupt_on_conflict,
        **{k: v for k, v in handler_kwargs.items() if k in valid_fields},
    )
    return OpenBoxDeepAgentHandler(graph, options)
