"""
OpenBox DeepAgents SDK — governance middleware for DeepAgents graphs.

Usage:
    >>> from openbox_deepagent import create_openbox_middleware
    >>> middleware = create_openbox_middleware(
    ...     api_url=os.environ["OPENBOX_URL"],
    ...     api_key=os.environ["OPENBOX_API_KEY"],
    ...     agent_name="MyBot",
    ...     known_subagents=["researcher"],
    ... )
    >>> agent = create_deep_agent(model="gpt-4o-mini", middleware=[middleware])
    >>> result = await agent.ainvoke({"messages": [...]})
"""

from importlib.metadata import PackageNotFoundError, version

# Re-export the openbox-langgraph-sdk public surface
from openbox_langgraph import (
    ApprovalExpiredError,
    ApprovalRejectedError,
    ApprovalTimeoutError,
    GovernanceBlockedError,
    GovernanceConfig,
    GovernanceHaltError,
    GovernanceVerdictResponse,
    GuardrailsValidationError,
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

from openbox_deepagent.middleware import OpenBoxMiddleware, OpenBoxMiddlewareOptions
from openbox_deepagent.middleware_factory import create_openbox_middleware
from openbox_deepagent.subagent_resolver import (
    DEEPAGENT_BUILTIN_TOOLS,
    DEEPAGENT_SUBAGENT_TOOL,
)

try:
    __version__ = version("openbox-deepagent-sdk-python")
except PackageNotFoundError:
    __version__ = "unknown"

__all__ = [
    # Shared
    "DEEPAGENT_BUILTIN_TOOLS",
    "DEEPAGENT_SUBAGENT_TOOL",
    "ApprovalExpiredError",
    "ApprovalRejectedError",
    "ApprovalTimeoutError",
    "GovernanceBlockedError",
    "GovernanceConfig",
    "GovernanceHaltError",
    "GovernanceVerdictResponse",
    "GuardrailsValidationError",
    "LangChainGovernanceEvent",
    "LangGraphStreamEvent",
    "OpenBoxAuthError",
    # Errors
    "OpenBoxError",
    "OpenBoxInsecureURLError",
    # Base handler
    "OpenBoxLangGraphHandler",
    "OpenBoxLangGraphHandlerOptions",
    # Middleware API
    "OpenBoxMiddleware",
    "OpenBoxMiddlewareOptions",
    "OpenBoxNetworkError",
    # Types
    "Verdict",
    # Version
    "__version__",
    "create_openbox_graph_handler",
    "create_openbox_middleware",
    "get_global_config",
    "initialize",
    "rfc3339_now",
    "safe_serialize",
]
