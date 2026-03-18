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
    >>> governed = create_openbox_deep_agent_handler(
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

from importlib.metadata import PackageNotFoundError, version

from openbox_deepagent.deepagent_handler import (
    DEEPAGENT_BUILTIN_TOOLS,
    DEEPAGENT_SUBAGENT_TOOL,
    OpenBoxDeepAgentHandler,
    OpenBoxDeepAgentHandlerOptions,
    create_openbox_deep_agent_handler,
)

# Re-export the openbox-langgraph-sdk public surface explicitly
from openbox_langgraph import (
    ApprovalExpiredError,
    ApprovalRejectedError,
    ApprovalTimeoutError,
    DEFAULT_HITL_CONFIG,
    GovernanceBlockedError,
    GovernanceConfig,
    GovernanceHaltError,
    GovernanceVerdictResponse,
    GuardrailsValidationError,
    HITLConfig,
    LangChainGovernanceEvent,
    LangGraphStreamEvent,
    OpenBoxAuthError,
    OpenBoxError,
    OpenBoxInsecureURLError,
    OpenBoxLangGraphHandler,
    OpenBoxLangGraphHandlerOptions,
    OpenBoxNetworkError,
    Verdict,
    create_openbox_graph_handler,
    get_global_config,
    initialize,
    rfc3339_now,
    safe_serialize,
)

try:
    __version__ = version("openbox-deepagent")
except PackageNotFoundError:
    __version__ = "unknown"

__all__ = [
    # DeepAgents-specific
    "OpenBoxDeepAgentHandler",
    "OpenBoxDeepAgentHandlerOptions",
    "create_openbox_deep_agent_handler",
    "DEEPAGENT_BUILTIN_TOOLS",
    "DEEPAGENT_SUBAGENT_TOOL",
    # Base handler
    "OpenBoxLangGraphHandler",
    "OpenBoxLangGraphHandlerOptions",
    "create_openbox_graph_handler",
    "initialize",
    "get_global_config",
    "GovernanceConfig",
    # Errors
    "OpenBoxError",
    "OpenBoxAuthError",
    "OpenBoxNetworkError",
    "OpenBoxInsecureURLError",
    "GovernanceBlockedError",
    "GovernanceHaltError",
    "GuardrailsValidationError",
    "ApprovalExpiredError",
    "ApprovalRejectedError",
    "ApprovalTimeoutError",
    # Types
    "Verdict",
    "HITLConfig",
    "DEFAULT_HITL_CONFIG",
    "LangGraphStreamEvent",
    "LangChainGovernanceEvent",
    "GovernanceVerdictResponse",
    "rfc3339_now",
    "safe_serialize",
    # Version
    "__version__",
]
