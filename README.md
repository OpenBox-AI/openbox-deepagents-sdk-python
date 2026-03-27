# openbox-deepagents-sdk-python

[![PyPI](https://img.shields.io/pypi/v/openbox-deepagent-sdk-python)](https://pypi.org/project/openbox-deepagent-sdk-python/)
[![Python](https://img.shields.io/pypi/pyversions/openbox-deepagent-sdk-python)](https://pypi.org/project/openbox-deepagent-sdk-python/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Add real-time governance to any [DeepAgents](https://github.com/langchain-ai/deepagents) application — powered by [OpenBox](https://openbox.ai).

This package extends [`openbox-langgraph-sdk-python`](https://pypi.org/project/openbox-langgraph-sdk-python/) with three things DeepAgents specifically needs: **per-subagent policy targeting** (govern the `writer` subagent differently from `researcher`), **HITL conflict detection** (prevent clashes with DeepAgents' own `interrupt_on`), and **built-in `a2a` tool classification** for subagent dispatches.

> **New to OpenBox?** Start with the [`openbox-langgraph-sdk-python` README](https://github.com/OpenBox-AI/openbox-langgraph-sdk-python). It covers policies, guardrails, HITL, error handling, and debugging. This document covers only what's different or additional for DeepAgents.

---

## Table of Contents

- [Architecture](#architecture)
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
- [Known limitations](#known-limitations)
- [Debugging](#debugging)
- [Contributing](#contributing)

---

## Architecture

The SDK implements LangChain's `AgentMiddleware` interface, hooking into the agent lifecycle at four points:

```
invoke() → abefore_agent  (SignalReceived → WorkflowStarted → pre-screen guardrails)
         → awrap_model_call (LLMStarted → PII redaction → model → LLMCompleted)
         → awrap_tool_call  (ToolStarted → tool execution with trace spans → ToolCompleted)
         → aafter_agent     (WorkflowCompleted + SpanProcessor cleanup)
```

### Core modules

| Module | Purpose |
|---|---|
| `middleware_factory.py` | `create_openbox_middleware()` factory — validates API key, sets global config, returns `OpenBoxMiddleware` |
| `middleware.py` | `OpenBoxMiddleware` class — sync/async hooks, span management, sync-to-async bridge |
| `middleware_hooks.py` | Stateless hook implementations — governance event construction, PII redaction, HITL retry loops |
| `subagent_resolver.py` | Subagent detection — extracts `subagent_type` from `task` tool args, HITL conflict detection |

### Key design decisions

- **Pre-screen optimization**: The first `LLMStarted` fires in `abefore_agent` and caches the response. `awrap_model_call` reuses it for the first LLM call to avoid a duplicate governance round-trip.
- **Tool classification**: `_resolve_tool_type()` checks `tool_type_map` first, falls back to `"a2a"` for subagent tools, and appends an `__openbox` sentinel to `activity_input` for Rego policy targeting.
- **Span bridging**: LangGraph spawns `asyncio.Task`s for tool/LLM execution, breaking trace context. The SDK manually creates spans and registers trace IDs with the `WorkflowSpanProcessor`.
- **Sync mode**: When `invoke()` (not `ainvoke()`) is used, governance calls switch to sync `httpx.Client` via `evaluate_event_sync()` to avoid context cancellation from `asyncio.run()` teardown.

### Dependency on openbox-langgraph-sdk-python

Heavy dependency — imports `GovernanceClient`, `GovernanceConfig`, `WorkflowSpanProcessor`, `enforce_verdict`, `poll_until_decision`, all error types, and `LangChainGovernanceEvent`. Changes to that package's internals directly affect this one.

---

## How DeepAgents governance works

DeepAgents dispatches subagents through the built-in `task` tool:

```python
task(description="Research quantum computing", subagent_type="researcher")
task(description="Write a technical report", subagent_type="writer")
```

The problem: subagents execute *inside* the `task` tool body, so their internal events are invisible to the outer LangGraph event stream. Only the `task` tool's start event is observable.

The SDK solves this by reading `subagent_type` from the `task` input before the call executes and embedding it as `__openbox` metadata in the governance event. Your Rego policy then has a clean, explicit handle to target specific subagent types:

```
Your agent                    SDK                           OpenBox Core
──────────                    ───                           ───────────
task(subagent_type="writer")
  │
  └─ wrap_tool_call ────────► ToolStarted                   Policy engine
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
                              (block / pause for HITL approval)
```

Your graph code is untouched.

---

## Installation

```bash
pip install openbox-deepagent-sdk-python
```

**Requirements:** Python 3.11+, `openbox-langgraph-sdk-python >= 0.1.0`, `langchain >= 0.3.0`, `langgraph >= 0.2`

---

## Quickstart

### 1. Create an agent in the dashboard

Sign in to [platform.openbox.ai](https://platform.openbox.ai), create an agent named `"ResearchBot"`, and copy your API key.

### 2. Export credentials

```bash
export OPENBOX_URL="https://core.openbox.ai"
export OPENBOX_API_KEY="obx_live_..."
```

### 3. Add OpenBox middleware to your agent

```python
import os
import asyncio
from deepagents import create_deep_agent
from langchain.chat_models import init_chat_model
from openbox_deepagent import create_openbox_middleware

# Create governance middleware
middleware = create_openbox_middleware(
    api_url=os.environ["OPENBOX_URL"],
    api_key=os.environ["OPENBOX_API_KEY"],
    agent_name="ResearchBot",       # must match the agent name in your dashboard
    known_subagents=["researcher", "analyst", "writer", "general-purpose"],
    tool_type_map={"search_web": "http", "export_data": "http"},
)

# Inject middleware into your DeepAgents graph — no wrapper needed
# IMPORTANT: do NOT pass interrupt_on if using OpenBox HITL (see HITL section)
agent = create_deep_agent(
    model=init_chat_model("openai:gpt-4o-mini", temperature=0),
    tools=[search_web, write_report, export_data],
    subagents=[
        {"name": "researcher", "description": "Web research.",
         "system_prompt": "You are a research assistant.", "tools": [search_web]},
        {"name": "writer", "description": "Drafting reports.",
         "system_prompt": "You are a professional writer.", "tools": [write_report]},
    ],
    middleware=[middleware],  # <-- governance injected here
)

async def main():
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "Research recent LangGraph papers"}]},
        config={"configurable": {"thread_id": "session-001"}},
    )
    print(result["messages"][-1].content)

asyncio.run(main())
```

---

## Configuration reference

`create_openbox_middleware()` accepts the following keyword arguments:

| Parameter | Type | Default | Description |
|---|---|---|---|
| `api_url` | `str` | **required** | Base URL of your OpenBox Core instance |
| `api_key` | `str` | **required** | API key (`obx_live_*` or `obx_test_*`) |
| `agent_name` | `str` | `None` | Agent name as configured in the dashboard. Used as `workflow_type` on all governance events — **must match exactly** for policies and Behavior Rules to fire |
| `known_subagents` | `list[str]` | `["general-purpose"]` | Subagent names from `create_deep_agent(subagents=[...])`. Always include `"general-purpose"` if the default subagent is active |
| `validate` | `bool` | `True` | Validate API key against server on startup |
| `on_api_error` | `str` | `"fail_open"` | `"fail_open"` (allow on error) or `"fail_closed"` (block on error) |
| `governance_timeout` | `float` | `30.0` | HTTP timeout in seconds for governance calls |
| `session_id` | `str` | `None` | Optional session identifier |
| `send_chain_start_event` | `bool` | `True` | Send `WorkflowStarted` event |
| `send_chain_end_event` | `bool` | `True` | Send `WorkflowCompleted` event |
| `send_llm_start_event` | `bool` | `True` | Send `LLMStarted` event (enables prompt guardrails + PII redaction) |
| `send_llm_end_event` | `bool` | `True` | Send `LLMCompleted` event |
| `send_tool_start_event` | `bool` | `True` | Send `ToolStarted` event |
| `send_tool_end_event` | `bool` | `True` | Send `ToolCompleted` event |
| `tool_type_map` | `dict[str, str]` | `{}` | Map tool names to semantic types for classification |
| `skip_tool_types` | `set[str]` | `set()` | Tool names to skip governance for entirely |
| `sqlalchemy_engine` | `Engine` | `None` | SQLAlchemy Engine instance to instrument for DB governance. Required when the engine is created before the middleware (see [Database governance](#database-governance)) |

---

## Governance features

### Policies (OPA / Rego)

Policies live in the OpenBox dashboard and are written in [Rego](https://www.openpolicyagent.org/docs/latest/policy-language/). Before every tool call the SDK sends a `ToolStarted` event — your policy evaluates the payload and returns a decision.

**Fields available in `input`:**

| Field | Type | Description |
|---|---|---|
| `input.event_type` | `string` | `"ToolStarted"`, `"ToolCompleted"`, `"LLMStarted"`, etc. |
| `input.activity_type` | `string` | Tool name (e.g. `"search_web"`, `"task"`) |
| `input.activity_input` | `array` | Tool arguments + optional `__openbox` metadata |
| `input.workflow_type` | `string` | Your `agent_name` |
| `input.workflow_id` | `string` | Session workflow ID |
| `input.trust_tier` | `int` | Agent trust tier (1–4) from dashboard |
| `input.hook_trigger` | `bool` | `true` when event is triggered by an outbound HTTP span |

**Example — block a restricted research topic:**

```rego
package org.openboxai.policy

import future.keywords.if
import future.keywords.in

default result = {"decision": "CONTINUE", "reason": null}

restricted_terms := {"nuclear weapon", "bioweapon", "chemical weapon", "malware synthesis"}

result := {"decision": "BLOCK", "reason": "Search blocked: restricted research topic."} if {
    input.event_type == "ToolStarted"
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

> **Always include `not input.hook_trigger`** in `BLOCK` and `REQUIRE_APPROVAL` rules. When a tool makes an outbound HTTP call, the SDK fires a second event with `hook_trigger: true`. Without this guard, the rule fires once for the tool call and once for every HTTP request it makes.

---

### Per-subagent policies

All subagent dispatches share the same `activity_type: "task"` — so a rule matching on `activity_type` can't distinguish a `writer` dispatch from a `researcher` one. The SDK appends a `__openbox` sentinel to `activity_input` to give your Rego policy that handle:

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

OpenBox Core forwards `activity_input` to OPA unchanged — **no Core changes needed**.

**Target a specific subagent:**

```rego
# All tasks dispatched to the writer subagent require human approval
result := {"decision": "REQUIRE_APPROVAL", "reason": "Writer subagent tasks require approval."} if {
    input.event_type == "ToolStarted"
    input.activity_type == "task"
    not input.hook_trigger
    some item in input.activity_input
    meta := item["__openbox"]
    meta.subagent_name == "writer"
}
```

**Target all subagent dispatches (any type):**

```rego
result := {"decision": "BLOCK", "reason": "Subagent calls disabled outside business hours."} if {
    input.event_type == "ToolStarted"
    input.activity_type == "task"
    not input.hook_trigger
    some item in input.activity_input
    item["__openbox"].tool_type == "a2a"
    # ... add time-based condition
}
```

> `subagent_name` is extracted from the `task` tool's `subagent_type` field automatically. If it's missing, the SDK falls back to `"general-purpose"` and logs a warning when `OPENBOX_DEBUG=1` is set.

---

### Guardrails

Guardrails screen LLM prompts before the model sees them. Configure them per agent in the dashboard.

Before each `ainvoke`, the SDK sends the user's message as an `LLMStarted` event to Core:

- **PII redaction** — matched fields are redacted in-place. The original text never reaches the model.
- **Content block** — `GuardrailsValidationError` is raised and the session halts before the graph starts.

Supported guardrail types:

| Type | ID | What it detects |
|---|---|---|
| PII detection | `1` | Names, emails, phone numbers, SSNs, credit cards |
| Content filter | `2` | Harmful or unsafe content categories |
| Toxicity | `3` | Toxic language |
| Ban words | `4` | Custom word/phrase blocklist |

> The SDK automatically skips governance for LLM calls with empty prompts (e.g. subagent-internal LLM calls with only system/tool messages), preventing guardrail parse errors.

---

### Human-in-the-loop (HITL)

When a policy returns `REQUIRE_APPROVAL`, the agent pauses and polls OpenBox until a human approves or rejects from the dashboard. The SDK handles this automatically — no additional configuration needed beyond the policy.

**On approval:** tool or subagent execution continues normally.
**On rejection:** `ApprovalRejectedError` is raised → re-raised as `GovernanceHaltError`.
**On expiry:** `ApprovalExpiredError` is raised → re-raised as `GovernanceHaltError`.

The HITL retry loop supports multiple approval rounds — if a tool triggers multiple governance hooks (e.g. httpx + file I/O), each gets its own approval cycle.

#### Conflict with DeepAgents `interrupt_on`

DeepAgents has its own interrupt mechanism via `interrupt_on` (`HumanInTheLoopMiddleware`). Using both OpenBox HITL and `interrupt_on` simultaneously causes double-pausing and unpredictable execution.

If you want OpenBox to own HITL (which gives you the full dashboard + audit trail), remove `interrupt_on`:

```python
# OpenBox owns HITL
agent = create_deep_agent(model="gpt-4o-mini", tools=[...], subagents=[...])

# conflict — don't do this
agent = create_deep_agent(model="gpt-4o-mini", tools=[...], interrupt_on=["task"])
```

---

### Behavior Rules (AGE)

> **Read [Known Limitations — Behavior Rules](#behavior-rules-count-task-dispatches-not-subagent-internal-tool-calls) before setting these up.** The semantics are materially different from the Temporal SDK, especially for DeepAgents.

Behavior Rules detect patterns across tool call sequences within a session — rate limits, unusual sequences, repeated high-risk dispatches. They're configured in the dashboard and enforced by the OpenBox Activity Governance Engine (AGE).

The SDK instruments `httpx` at startup (one-time idempotent patch). Any `httpx` call a tool makes is captured as a span and attached to that tool's `ToolCompleted` event.

---

### Tool classification

Map your non-subagent tools to semantic types so your Rego policies can target whole categories instead of listing every tool name.

```python
middleware = create_openbox_middleware(
    api_url=os.environ["OPENBOX_URL"],
    api_key=os.environ["OPENBOX_API_KEY"],
    agent_name="ResearchBot",
    known_subagents=["researcher", "writer", "general-purpose"],
    tool_type_map={
        "search_web": "http",
        "export_data": "http",
        "query_db":    "database",
    },
)
```

**Supported `tool_type` values:** `"http"`, `"database"`, `"builtin"`, `"a2a"`

> `"a2a"` is set automatically on every `task` call when `subagent_name` is resolved. Don't add `"task"` to `tool_type_map`.

When a type is set, the SDK appends an `__openbox` sentinel to `activity_input`:

```json
{"__openbox": {"tool_type": "http"}}
```

Rego can match on it:

```rego
result := {"decision": "REQUIRE_APPROVAL", "reason": "HTTP calls require approval."} if {
    input.event_type == "ToolStarted"
    not input.hook_trigger
    some item in input.activity_input
    item["__openbox"].tool_type == "http"
}
```

---

### Database governance

The SDK instruments database operations via automatic tracing. Supported libraries: psycopg2, asyncpg, mysql, pymysql, sqlite3, pymongo, redis, sqlalchemy.

Install the instrumentation package for your database:

```bash
pip install opentelemetry-instrumentation-sqlite3      # SQLite
pip install opentelemetry-instrumentation-psycopg2     # PostgreSQL
pip install opentelemetry-instrumentation-sqlalchemy    # SQLAlchemy ORM
```

**Important: initialization order.** If your database connection or SQLAlchemy engine is created **before** `create_openbox_middleware()`, pass the engine explicitly:

```python
from langchain_community.utilities import SQLDatabase

# Engine created here (before middleware)
db = SQLDatabase.from_uri("sqlite:///Chinook.db")

# Pass engine so the SDK can instrument it retroactively
middleware = create_openbox_middleware(
    api_url=os.environ["OPENBOX_URL"],
    api_key=os.environ["OPENBOX_API_KEY"],
    agent_name="TextToSQL",
    sqlalchemy_engine=db._engine,  # <-- instrument existing engine
)
```

Without `sqlalchemy_engine=`, only engines created **after** middleware initialization are instrumented.

---

## Error handling

All governance exceptions are importable from `openbox_deepagent`:

```python
from openbox_deepagent import (
    GovernanceBlockedError,
    GovernanceHaltError,
    GuardrailsValidationError,
    ApprovalRejectedError,
    ApprovalExpiredError,
)

try:
    result = await agent.ainvoke({"messages": [...]}, config=...)
except GovernanceBlockedError as e:
    # Policy returned BLOCK — tool or subagent dispatch did not execute
    print(f"Blocked: {e}")
except GovernanceHaltError as e:
    # Policy returned HALT, or a HITL decision was rejected/expired
    print(f"Session halted: {e}")
except GuardrailsValidationError as e:
    # Guardrail fired on the user prompt — graph never started
    print(f"Guardrail: {e}")
except ApprovalRejectedError as e:
    print(f"Rejected by reviewer: {e}")
except ApprovalExpiredError as e:
    print(f"HITL expired: {e}")
```

| Exception | Raised when |
|---|---|
| `GovernanceBlockedError` | Policy returned `BLOCK` |
| `GovernanceHaltError` | Policy returned `HALT`, or a HITL decision was rejected or expired |
| `GuardrailsValidationError` | A guardrail fired on the user prompt |
| `ApprovalRejectedError` | A human explicitly rejected a `REQUIRE_APPROVAL` decision |
| `ApprovalExpiredError` | HITL approval expired server-side |
| `OpenBoxAuthError` | API key is invalid or unauthorized |

---

## Advanced usage

### Multi-turn sessions

Use a stable `thread_id` across turns. The SDK generates a fresh `workflow_id` per call internally — your code just passes the same `thread_id`:

```python
config = {"configurable": {"thread_id": "user-42-session-7"}}

await agent.ainvoke(
    {"messages": [{"role": "user", "content": "Research LangGraph"}]},
    config=config,
)
await agent.ainvoke(
    {"messages": [{"role": "user", "content": "Now write a report on it"}]},
    config=config,
)
```

### Inspecting registered subagents

```python
print(middleware.get_known_subagents())
# ['analyst', 'general-purpose', 'researcher', 'writer']
```

### `fail_closed` mode

```python
middleware = create_openbox_middleware(
    api_url=os.environ["OPENBOX_URL"],
    api_key=os.environ["OPENBOX_API_KEY"],
    agent_name="ResearchBot",
    on_api_error="fail_closed",
)
```

### Sync mode

The middleware supports both `ainvoke()` (async) and `invoke()` (sync). When using sync mode, governance calls automatically use sync `httpx.Client` and a cached thread pool to avoid `asyncio.run()` teardown issues.

```python
# Async (preferred)
result = await agent.ainvoke({"messages": [...]}, config=config)

# Sync (also supported)
result = agent.invoke({"messages": [...]}, config=config)
```

---

## Known limitations

These constraints come from how DeepAgents and LangGraph work at runtime. The base limitations are covered in the [openbox-langgraph-sdk-python README](https://github.com/OpenBox-AI/openbox-langgraph-sdk-python). These are the DeepAgents-specific additions.

### Subagent internals are invisible to governance

Subagents execute *inside* the `task` tool body via `subagent.invoke()`. Their internal tool calls and LLM calls are not surfaced in the outer event stream. From the governance layer, the `task` call is a single atomic unit.

**What this means concretely:**
- A `search_web` call made by the `researcher` subagent is not a separate `ToolStarted` event — you cannot write a Rego policy that targets it
- You cannot apply HITL to a tool call a subagent makes — only to the `task` dispatch itself
- The `ToolCompleted` for `task` carries the final output, but not a breakdown of what the subagent did internally

**What you can govern:**
- Whether a specific subagent type is dispatched at all (`BLOCK` / `REQUIRE_APPROVAL` on `activity_type == "task"` with `subagent_name` matching)
- Patterns in how many times each subagent type is dispatched per session

**Workaround:** If you need to govern a specific tool call regardless of which subagent triggers it, add it to the outer agent's tool list as well. The outer agent's tool calls are fully governed.

---

### Behavior Rules count task dispatches, not subagent-internal tool calls

The AGE sees `task(subagent_type="researcher")` as one `ToolStarted` + one `ToolCompleted`. The researcher then calling `search_web` five times internally is invisible.

A rule like "block if `search_web` exceeds 10 calls per session" only counts direct `search_web` calls from the outer agent — not from subagents.

**What works reliably for DeepAgents:**
- Rate-limiting `task` dispatches per subagent type (e.g. researcher called more than 5 times)
- Rate-limiting total subagent dispatches
- Detecting unusual outer-agent tool sequences

---

### HTTP spans are captured for outer agent tools only

The `httpx` instrumentation captures calls made during outer agent tool execution. HTTP calls inside subagent tool bodies run in a separate async context and are not captured as spans on the `task` `ToolCompleted`.

---

### Behavior Rules don't span `ainvoke` calls

Each `ainvoke` is a separate governance session with a new `workflow_id`. Behavior Rules track patterns **within a single invocation only**. Cross-turn pattern detection is not yet supported.

---

## Debugging

```bash
OPENBOX_DEBUG=1 python agent.py
```

This enables debug logging:

```
[OpenBox Debug] governance request: {
  "event_type": "ToolStarted",
  "activity_type": "task",
  "workflow_type": "ResearchBot",
  "activity_input": [
    {"description": "Write a report on AI safety", "subagent_type": "writer"},
    {"__openbox": {"tool_type": "a2a", "subagent_name": "writer"}}
  ],
  "hook_trigger": false
}
[OpenBox Debug] governance response: { "verdict": "require_approval", "reason": "Writer tasks require approval." }
```

**If things aren't working, check for these:**

- `workflow_type` doesn't match your dashboard agent name → policies never fire
- `subagent_name` is `"general-purpose"` when you expected something else → `subagent_type` was missing from the `task` input; look for a `task tool_call missing subagent_type` warning in the debug output
- A rule is double-triggering → you're missing `not input.hook_trigger` in your Rego
- Warning at startup about `known_subagents` → you passed an empty list; include at least `["general-purpose"]`

---

## Contributing

Contributions are welcome! Please open an issue before submitting a large pull request.

```bash
git clone https://github.com/OpenBox-AI/openbox-deepagents-sdk-python
cd openbox-deepagents-sdk-python
uv sync --extra dev
uv run pytest tests/ -v
uv run ruff check openbox_deepagent/ tests/
```
