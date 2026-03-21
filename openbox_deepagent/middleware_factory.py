"""Factory for creating configured OpenBoxMiddleware instances.

Usage:
    middleware = create_openbox_middleware(
        api_url=os.environ["OPENBOX_URL"],
        api_key=os.environ["OPENBOX_API_KEY"],
        agent_name="ResearchBot",
        known_subagents=["researcher", "writer"],
    )
    agent = create_deep_agent(
        model=init_chat_model("openai:gpt-4o-mini"),
        middleware=[middleware],
        tools=[...],
    )
    result = await agent.ainvoke({"messages": [...]})
"""

from __future__ import annotations

import dataclasses
from typing import Any

from openbox_deepagent.middleware import OpenBoxMiddleware, OpenBoxMiddlewareOptions


def create_openbox_middleware(
    *,
    api_url: str,
    api_key: str,
    agent_name: str | None = None,
    governance_timeout: float = 30.0,
    validate: bool = True,
    known_subagents: list[str] | None = None,
    sqlalchemy_engine: Any = None,
    **kwargs: Any,
) -> OpenBoxMiddleware:
    """Create a configured OpenBoxMiddleware for create_deep_agent(middleware=[...]).

    Validates the API key and sets up global config before returning the middleware.

    Args:
        api_url: Base URL of your OpenBox Core instance.
        api_key: API key in ``obx_live_*`` or ``obx_test_*`` format.
        agent_name: Agent name as configured in the dashboard.
        governance_timeout: HTTP timeout in seconds for governance calls (default 30.0).
        validate: If True, validates the API key against the server on startup.
        known_subagents: Subagent names from ``create_deep_agent(subagents=[...])``.
            Defaults to ``["general-purpose"]``.
        sqlalchemy_engine: Optional SQLAlchemy Engine instance to instrument for DB
            governance. Required when the engine is created before the middleware
            (e.g. ``SQLDatabase.from_uri()``). Without this, only engines created
            after middleware initialization will be instrumented.
        **kwargs: Additional keyword arguments forwarded to ``OpenBoxMiddlewareOptions``.

    Returns:
        A configured ``OpenBoxMiddleware`` ready for injection into create_deep_agent.
    """
    from openbox_langgraph.config import initialize
    initialize(
        api_url=api_url,
        api_key=api_key,
        governance_timeout=governance_timeout,
        validate=validate,
    )

    valid_fields = {f.name for f in dataclasses.fields(OpenBoxMiddlewareOptions)}
    options = OpenBoxMiddlewareOptions(
        agent_name=agent_name,
        governance_timeout=governance_timeout,
        known_subagents=known_subagents or ["general-purpose"],
        sqlalchemy_engine=sqlalchemy_engine,
        **{k: v for k, v in kwargs.items() if k in valid_fields},
    )
    return OpenBoxMiddleware(options)
