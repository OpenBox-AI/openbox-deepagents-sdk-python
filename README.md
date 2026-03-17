# openbox-deepagent

[![PyPI](https://img.shields.io/pypi/v/openbox-deepagent)](https://pypi.org/project/openbox-deepagent/)
[![Python](https://img.shields.io/pypi/pyversions/openbox-deepagent)](https://pypi.org/project/openbox-deepagent/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Real-time governance and observability for [DeepAgents](https://github.com/langchain-ai/deepagents) — powered by [OpenBox](https://openbox.ai).

This package extends [`openbox-langgraph-sdk`](../sdk-langgraph-python) with governance features specific to the DeepAgents framework: **per-subagent policy targeting**, **HITL conflict detection**, and **built-in tool classification**.

---

## Table of Contents

- [How DeepAgents governance works](#how-deepagents-governance-works)
- [Installation](#installation)
- [Quickstart](#quickstart)
- [Configuration reference](#configuration-reference)
- [Governance features](#governance-features)
  - [Policies (OPA / Rego)](#policies-opa--rego)
  - [Per-subagent policies](#per-subagent-policies)
  - [Guardrails](#guardrails)
  - [Human-in-the-loop (HITL)](#human-in-the-loop-hitl)
  - [Behavior Rules (AGE)](#behavior-rules-age)
  - [Tool classification](#tool-classification)
- [Error handling](#error-handling)
- [Advanced usage](#advanced-usage)
- [Debugging](#debugging)
- [Contributing](#contributing)

---

## How DeepAgents governance works

DeepAgents orchestrates work through named subagents invoked via the built-in `task` tool:

```python
task(description="Research quantum computing", subagent_type="researcher")
task(description="Write a technical report", subagent_type="writer")
```

**The key challenge:** subagents run *synchronously inside* the `task` tool body. Their internal LangGraph events are invisible to the outer agent stream — only the `task` tool's `on_tool_start` event is observable.

`openbox-deepagent` solves this by **intercepting the `task` tool's input** and extracting `subagent_type` before the call executes. This `subagent_name` is embedded into the governance event, allowing Rego policies to target specific subagent types precisely.

```
Your agent                    SDK                           OpenBox Core
──────────                    ───                           ────────────
task(subagent_type="writer")
  │
  └─ on_tool_start ────────► ActivityStarted               Policy engine
                              activity_type="task"    ───► input.activity_type == "task"
                              activity_input=[                some item in input.activity_input
                                {description, subagent_type},  item["__openbox"].subagent_name
                                {__openbox: {                    == "writer"
                                  tool_type: "a2a",         ◄─── REQUIRE_APPROVAL
                                  subagent_name: "writer"
                                }}
                              ]
                                    ↑
                              enforce verdict
                              (block / pause for approval)
```

**Zero graph changes required.** Wrap your existing `create_deep_agent()` graph — the handler does the rest.

---

## Installation

```bash
pip install openbox-deepagent
```

**Requirements:** Python 3.11+, `openbox-langgraph-sdk`, `langgraph >= 0.2`, `deepagents`

---

## Quickstart

### 1. Get your API key

Sign in to [dashboard.openbox.ai](https://dashboard.openbox.ai), create an agent called `"ResearchBot"`, and copy your API key.

### 2. Set environment variables

```bash
export OPENBOX_URL="https://core.openbox.ai"
export OPENBOX_API_KEY="obx_live_..."
```

### 3. Wrap your DeepAgents graph

```python
import os
import asyncio
from deepagents import create_deep_agent
from langchain.chat_models import init_chat_model
from openbox_deepagent import create_openbox_deep_agent_handler

# Your existing DeepAgents graph — no changes needed
# IMPORTANT: do NOT pass interrupt_on if using OpenBox HITL (see HITL section)
agent = create_deep_agent(
    model=init_chat_model("openai:gpt-4o-mini", temperature=0),
    tools=[search_web, write_report, export_data],
    subagents=[
        {"name": "researcher", "description": "Web research and summarization.",
         "system_prompt": "You are a research assistant.", "tools": [search_web]},
        {"name": "analyst",    "description": "Data analysis and comparison.",
         "system_prompt": "You are a data analyst.",       "tools": [search_web]},
        {"name": "writer",     "description": "Drafting reports and documents.",
         "system_prompt": "You are a professional writer.", "tools": [write_report]},
    ],
)

async def main():
    governed = await create_openbox_deep_agent_handler(
        graph=agent,
        api_url=os.environ["OPENBOX_URL"],
        api_key=os.environ["OPENBOX_API_KEY"],
        agent_name="ResearchBot",       # must match the agent name in your dashboard
        known_subagents=["researcher", "analyst", "writer", "general-purpose"],
    )

    result = await governed.ainvoke(
        {"messages": [{"role": "user", "content": "Research recent LangGraph papers"}]},
        config={"configurable": {"thread_id": "session-001"}},
    )
    print(result["messages"][-1].content)

asyncio.run(main())
```

---

## Configuration reference

`create_openbox_deep_agent_handler` accepts the following keyword arguments:

| Parameter | Type | Default | Description |
|---|---|---|---|
| `graph` | `CompiledGraph` | **required** | Compiled LangGraph graph from `create_deep_agent()` |
| `api_url` | `str` | **required** | Base URL of your OpenBox Core instance |
| `api_key` | `str` | **required** | API key (`obx_live_*` or `obx_test_*`) |
| `agent_name` | `str` | `None` | Agent name as configured in the dashboard |
| `known_subagents` | `list[str]` | `["general-purpose"]` | Subagent names from `create_deep_agent(subagents=[...])` |
| `validate` | `bool` | `True` | Validate API key against server on startup |
| `on_api_error` | `str` | `"fail_open"` | `"fail_open"` or `"fail_closed"` |
| `api_timeout` | `float` | `30.0` | HTTP timeout in seconds for governance calls |
| `session_id` | `str` | `None` | Optional session identifier |
| `hitl` | `dict` | `{}` | Human-in-the-loop config (see [HITL](#human-in-the-loop-hitl)) |
| `guard_interrupt_on_conflict` | `bool` | `True` | Raise if `interrupt_on` and OpenBox HITL are both enabled |
| `tool_type_map` | `dict[str, str]` | `{}` | Map tool names to semantic types for classification |
| `skip_chain_types` | `set[str]` | `set()` | Chain node names to skip |
| `skip_tool_types` | `set[str]` | `set()` | Tool names to skip entirely |

---

## Governance features

### Policies (OPA / Rego)

Policies are written in [Rego](https://www.openpolicyagent.org/docs/latest/policy-language/) and configured in the OpenBox dashboard under your agent. The SDK sends an `ActivityStarted` event before every tool call; your policy decides what happens next.

**Fields available in `input`:**

| Field | Type | Description |
|---|---|---|
| `input.event_type` | `string` | `"ActivityStarted"` or `"ActivityCompleted"` |
| `input.activity_type` | `string` | Tool name (e.g. `"search_web"`, `"task"`) |
| `input.activity_input` | `array` | Tool arguments + optional `__openbox` metadata |
| `input.workflow_type` | `string` | Your `agent_name` |
| `input.workflow_id` | `string` | Session workflow ID |
| `input.trust_tier` | `int` | Agent trust tier (1–4) from dashboard |
| `input.hook_trigger` | `bool` | `true` when event is a hook-level HTTP re-evaluation |

**Example — block a restricted research topic:**

```rego
package org.openboxai.policy

import future.keywords.if
import future.keywords.in

default result = {"decision": "CONTINUE", "reason": null}

restricted_terms := {"nuclear weapon", "bioweapon", "chemical weapon", "malware synthesis"}

result := {"decision": "BLOCK", "reason": "Search blocked: restricted research topic."} if {
    input.event_type == "ActivityStarted"
    input.activity_type == "search_web"
    not input.hook_trigger
    count(input.activity_input) > 0
    entry := input.activity_input[0]
    is_object(entry)
    query := entry.query
    is_string(query)
    some term in restricted_terms
    contains(lower(query), term)
}
```

**Possible decisions:**

| Decision | Effect |
|---|---|
| `CONTINUE` | Tool executes normally |
| `BLOCK` | `GovernanceBlockedError` raised — tool does not execute |
| `REQUIRE_APPROVAL` | Agent pauses; human must approve or reject in dashboard |
| `HALT` | `GovernanceHaltError` raised — session terminated |

> **Always add `not input.hook_trigger`** to `BLOCK` and `REQUIRE_APPROVAL` rules. The SDK's HTTP telemetry layer intercepts outgoing HTTP calls and sends a second `ActivityStarted` event (with `hook_trigger: true`). Without this guard, those rules will fire twice — once for the tool call and once for the underlying HTTP request.

---

### Per-subagent policies

This is the key feature of `openbox-deepagent`. Because all subagent dispatches go through the `task` tool, you can't distinguish a `writer` task from a `researcher` task using `activity_type` alone. The SDK solves this by appending a `__openbox` metadata sentinel to `activity_input`:

```json
"activity_input": [
  {
    "description": "Write a report on AI safety",
    "subagent_type": "writer"
  },
  {
    "__openbox": {
      "tool_type": "a2a",
      "subagent_name": "writer"
    }
  }
]
```

This works entirely through `activity_input`, which OpenBox Core already forwards to OPA unchanged — **no Core changes needed**.

**Rego rule targeting a specific subagent:**

```rego
# All tasks dispatched to the writer subagent require human approval
result := {"decision": "REQUIRE_APPROVAL", "reason": "Writer subagent tasks require approval."} if {
    input.event_type == "ActivityStarted"
    input.activity_type == "task"
    not input.hook_trigger
    some item in input.activity_input
    meta := item["__openbox"]
    meta.subagent_name == "writer"
}
```

Because the sentinel is appended for **every** subagent dispatch (not just writer), you can also write rules targeting any subagent type — or all of them:

```rego
# Block all A2A subagent calls during off-hours (example)
result := {"decision": "BLOCK", "reason": "Subagent calls are disabled outside business hours."} if {
    input.event_type == "ActivityStarted"
    input.activity_type == "task"
    not input.hook_trigger
    some item in input.activity_input
    item["__openbox"].tool_type == "a2a"
    # ... time-based condition
}
```

> **`subagent_name` is extracted automatically** from `task` tool's `subagent_type` input field. You do not need to configure anything extra beyond listing the subagents in `known_subagents`.

---

### Guardrails

Guardrails screen LLM prompts and tool outputs. Configure them in the dashboard per agent.

Supported guardrail types:

| Type | ID | What it detects |
|---|---|---|
| PII detection | `1` | Names, emails, phone numbers, SSNs, credit cards |
| Content filter | `2` | Harmful or unsafe content categories |
| Toxicity | `3` | Toxic language |
| Ban words | `4` | Custom word/phrase blocklist |
| Regex | `5` | Custom regex patterns |

When a guardrail fires on an LLM prompt:
- **PII redaction** — the prompt is automatically redacted before the LLM sees it, in-place
- **Content block** — `GuardrailsValidationError` is raised and the session halts

---

### Human-in-the-loop (HITL)

When a policy returns `REQUIRE_APPROVAL`, the agent pauses and polls OpenBox for a human decision. The human approves or rejects from the OpenBox dashboard.

```python
governed = await create_openbox_deep_agent_handler(
    graph=agent,
    api_url=os.environ["OPENBOX_URL"],
    api_key=os.environ["OPENBOX_API_KEY"],
    agent_name="ResearchBot",
    known_subagents=["researcher", "analyst", "writer", "general-purpose"],
    hitl={
        "enabled": True,
        "poll_interval_ms": 5_000,   # check every 5 seconds
        "max_wait_ms": 300_000,      # timeout after 5 minutes
    },
)
```

**HITL config options:**

| Key | Type | Default | Description |
|---|---|---|---|
| `enabled` | `bool` | `False` | Enable HITL polling |
| `poll_interval_ms` | `int` | `5000` | How often to poll for a decision (ms) |
| `max_wait_ms` | `int` | `300000` | Total wait before `ApprovalTimeoutError` (ms) |

#### Conflict with DeepAgents `interrupt_on`

DeepAgents supports its own HITL mechanism via `interrupt_on` (the `HumanInTheLoopMiddleware`). **Do not use both at the same time** — they conflict, causing double-pausing and unpredictable behavior.

The SDK detects this automatically and raises a `ValueError` at construction time if both are enabled:

```
[OpenBox] DeepAgents graph has interrupt_on (HumanInTheLoopMiddleware) configured
AND OpenBox HITL is enabled. These conflict — OpenBox must own the HITL flow.
Remove interrupt_on from create_deep_agent, or set guard_interrupt_on_conflict=False to skip this check.
```

**Rule of thumb:** If you want OpenBox to manage HITL (recommended — it gives you the full dashboard + audit trail), remove `interrupt_on` from `create_deep_agent`:

```python
# ✅ correct — OpenBox owns HITL
agent = create_deep_agent(model="gpt-4o-mini", tools=[...], subagents=[...])

# ❌ conflict — both will try to pause execution
agent = create_deep_agent(model="gpt-4o-mini", tools=[...], interrupt_on=["task"])
```

---

### Behavior Rules (AGE)

Behavior Rules detect patterns across sequences of tool calls within a session. Configured in the dashboard and enforced by the OpenBox Activity Governance Engine (AGE).

Example use cases:
- Flag if the researcher subagent is called more than 10 times in one session
- Detect when the agent alternates between `search_web` and `export_data` repeatedly (exfiltration pattern)
- Rate-limit external HTTP calls per session

The SDK automatically captures HTTP spans (via `httpx` hooks) from tools that make outbound HTTP requests and sends them with `ActivityCompleted` events.

---

### Tool classification

Classify your non-subagent tools into semantic categories to enable category-level Rego rules and richer execution tree labels in the dashboard.

```python
governed = await create_openbox_deep_agent_handler(
    graph=agent,
    agent_name="ResearchBot",
    known_subagents=["researcher", "analyst", "writer", "general-purpose"],
    tool_type_map={
        "search_web": "http",
        "export_data": "http",
        "query_db":    "database",
    },
    ...
)
```

**Supported `tool_type` values:** `"http"`, `"database"`, `"builtin"`, `"a2a"`

> **`"a2a"` is set automatically** for every `task` tool call when `subagent_name` is resolved. You do not need to add `"task"` to `tool_type_map`.

When a type is set, the SDK appends an `__openbox` sentinel to `activity_input`:

```json
{"__openbox": {"tool_type": "http"}}
```

Rego can match on it:

```rego
# Flag any tool making outbound HTTP calls
result := {"decision": "REQUIRE_APPROVAL", "reason": "HTTP calls require approval in this environment."} if {
    input.event_type == "ActivityStarted"
    not input.hook_trigger
    some item in input.activity_input
    item["__openbox"].tool_type == "http"
}
```

---

## Error handling

```python
from openbox_deepagent import (
    GovernanceBlockedError,
    GovernanceHaltError,
    GuardrailsValidationError,
    ApprovalRejectedError,
    ApprovalTimeoutError,
)

try:
    result = await governed.ainvoke({"messages": [...]}, config=...)
except GovernanceBlockedError as e:
    print(f"Action blocked by policy: {e}")
except GovernanceHaltError as e:
    print(f"Session halted: {e}")
except GuardrailsValidationError as e:
    print(f"Guardrail triggered: {e}")
except ApprovalRejectedError as e:
    print(f"Human rejected the action: {e}")
except ApprovalTimeoutError as e:
    print(f"HITL approval timed out: {e}")
```

| Exception | When raised |
|---|---|
| `GovernanceBlockedError` | Policy returned `BLOCK` |
| `GovernanceHaltError` | Policy returned `HALT`, or HITL was rejected/expired |
| `GuardrailsValidationError` | Guardrail fired on an LLM prompt or tool output |
| `ApprovalRejectedError` | Human rejected a `REQUIRE_APPROVAL` decision |
| `ApprovalTimeoutError` | HITL polling exceeded `max_wait_ms` |

---

## Advanced usage

### Streaming

```python
async for event in governed.astream_governed(
    {"messages": [{"role": "user", "content": "Research quantum computing"}]},
    config={"configurable": {"thread_id": "session-001"}},
    stream_mode="values",
):
    pass
```

### Multi-turn sessions

Pass a consistent `thread_id` across turns:

```python
config = {"configurable": {"thread_id": "user-42-session-7"}}

await governed.ainvoke({"messages": [{"role": "user", "content": "Research LangGraph"}]}, config=config)
await governed.ainvoke({"messages": [{"role": "user", "content": "Now write a report on it"}]}, config=config)
```

### Inspecting registered subagents

```python
governed = await create_openbox_deep_agent_handler(
    graph=agent,
    known_subagents=["researcher", "analyst", "writer"],
    ...
)

print(governed.get_known_subagents())
# ['analyst', 'researcher', 'writer']
```

### `fail_closed` mode

For high-sensitivity environments, use `on_api_error="fail_closed"` to block all tool calls if OpenBox Core is unreachable:

```python
governed = await create_openbox_deep_agent_handler(
    graph=agent,
    on_api_error="fail_closed",
    ...
)
```

### Skipping internal chain events

`create_deep_agent()` emits `on_chain_start` events for internal middleware nodes. Skip these to reduce governance noise. The recommended set covers the common DeepAgents middleware node names:

```python
governed = await create_openbox_deep_agent_handler(
    graph=agent,
    skip_chain_types={
        "model",                                    # LLM wrapper node
        "tools",                                    # tool container node
        "PatchToolCallsMiddleware.before_agent",
        "TodoListMiddleware.after_model",
        "FilesystemMiddleware.before_agent",
        "SummarizationMiddleware.before_agent",
        "AnthropicPromptCachingMiddleware.before_agent",
        "SubAgentMiddleware.before_agent",
        "MemoryMiddleware.before_agent",
        "SkillsMiddleware.before_agent",
    },
    ...
)
```

> **Why?** Individual `on_tool_start`/`on_tool_end` events fire inside the `tools` node — skipping the container node does not suppress tool governance. Similarly, `on_chat_model_start`/`on_chat_model_end` fire inside `model` — skipping the wrapper node does not suppress LLM governance.

To discover the exact node names your graph emits, run with `OPENBOX_DEBUG=1` and look for `[OBX_EVENT]` lines.

---

## Debugging

Set `OPENBOX_DEBUG=1` to log all governance requests/responses and every raw LangGraph event the SDK processes:

```bash
OPENBOX_DEBUG=1 python agent.py
```

Two output streams:

**`[OBX_EVENT]`** — every raw LangGraph event (to stderr):
```
[OBX_EVENT] on_chain_start             name='LangGraph'                  node=None
[OBX_EVENT] on_chain_start             name='PatchToolCallsMiddleware...' node='PatchToolCallsMiddleware...'
[OBX_EVENT] on_chat_model_start        name='ChatOpenAI'                  node='model'
[OBX_EVENT] on_tool_start              name='task'                        node='tools'
[OBX_EVENT] on_tool_start              name='search_web'                  node='tools'
```

**`[OpenBox Debug]`** — governance requests/responses (to stdout):
```
[OpenBox Debug] governance request: {
  "event_type": "ActivityStarted",
  "activity_type": "task",
  "activity_input": [
    {"description": "Write a report on AI safety", "subagent_type": "writer"},
    {"__openbox": {"tool_type": "a2a", "subagent_name": "writer"}}
  ]
}
[OpenBox Debug] governance response: {
  "verdict": "require_approval",
  "reason": "Writer subagent tasks require approval."
}
```

#### Empty prompt handling

DeepAgents emits `on_chat_model_start` for **every** LLM invocation — including internal LLM calls that may not include a human turn message. Empty prompts are skipped for `agent_validatePrompt` governance to avoid guardrail parse errors (for example: `Expecting value: line 1 column 1 (char 0)`). Only prompts that include a user turn are evaluated by prompt guardrails.

---

## Contributing

Contributions are welcome! Please open an issue before submitting a large pull request.

```bash
git clone https://github.com/openbox-ai/openbox-langchain-sdk
cd sdk-deepagent-python
pip install -e ".[dev]"
pytest
```
