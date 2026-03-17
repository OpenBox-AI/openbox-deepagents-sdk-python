# openbox-deepagent

OpenBox governance and observability SDK for DeepAgents (langchain-ai/deepagents).

Extends `openbox-langgraph-sdk` with DeepAgents-specific governance:
- Subagent detection via the `task` tool's `subagent_type` argument
- `interrupt_on` conflict guard (DeepAgents HITL vs OpenBox HITL)
- Built-in tool classification for Rego policy targeting

## Installation

```bash
pip install openbox-deepagent
```

## Usage

```python
from openbox_deepagent import create_openbox_deep_agent_handler

governed = await create_openbox_deep_agent_handler(
    graph=agent,  # compiled LangGraph graph from create_deep_agent()
    api_url="https://core.openbox.ai",
    api_key="obx_live_...",
    agent_name="MyDeepAgent",
    known_subagents=["general-purpose", "researcher"],
)

result = await governed.ainvoke(
    {"messages": [{"role": "user", "content": "Research LangGraph"}]},
    config={"configurable": {"thread_id": "session-001"}},
)
```
