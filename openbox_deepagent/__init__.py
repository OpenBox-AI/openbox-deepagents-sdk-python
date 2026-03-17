"""
OpenBox DeepAgents SDK — extends openbox-langgraph-sdk for DeepAgents graphs.

Adds DeepAgents-specific governance:
- Subagent detection via the `task` tool's `subagent_type` argument
- interrupt_on conflict guard (DeepAgents HumanInTheLoopMiddleware vs OpenBox HITL)
- Built-in tool classification for Rego policy targeting

Example:
    >>> from openbox_deepagent import create_openbox_deep_agent_handler
    >>> from deepagents.graph import create_deep_agent
    >>>
    >>> # IMPORTANT: do NOT pass interrupt_on to create_deep_agent when using OpenBox HITL
    >>> agent = create_deep_agent(model="claude-sonnet-4-5", tools=[my_tool])
    >>>
    >>> governed = await create_openbox_deep_agent_handler(
    ...     graph=agent,
    ...     api_url=os.environ["OPENBOX_URL"],
    ...     api_key=os.environ["OPENBOX_API_KEY"],
    ...     agent_name="MyDeepAgent",
    ...     known_subagents=["general-purpose", "researcher"],
    ... )
    >>> result = await governed.ainvoke(
    ...     {"messages": [{"role": "user", "content": "Research LangGraph"}]},
    ...     config={"configurable": {"thread_id": "session-abc"}},
    ... )
"""

from openbox_deepagent.deepagent_handler import (
    DEEPAGENT_BUILTIN_TOOLS,
    DEEPAGENT_SUBAGENT_TOOL,
    OpenBoxDeepAgentHandler,
    OpenBoxDeepAgentHandlerOptions,
    create_openbox_deep_agent_handler,
)

# Re-export the full langgraph SDK surface
from openbox_langgraph import *  # noqa: F401, F403
from openbox_langgraph import (
    OpenBoxLangGraphHandler,
    OpenBoxLangGraphHandlerOptions,
    create_openbox_graph_handler,
)

__all__ = [
    # DeepAgents-specific
    "OpenBoxDeepAgentHandler",
    "OpenBoxDeepAgentHandlerOptions",
    "create_openbox_deep_agent_handler",
    "DEEPAGENT_BUILTIN_TOOLS",
    "DEEPAGENT_SUBAGENT_TOOL",
    # Base handler re-exports
    "OpenBoxLangGraphHandler",
    "OpenBoxLangGraphHandlerOptions",
    "create_openbox_graph_handler",
]
