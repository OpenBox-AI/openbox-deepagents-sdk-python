# Codebase Summary — openbox-deepagent

## Project Statistics

- **Total Lines of Code**: 2,288 LOC (excl. test venv, .idea/)
- **Python Modules**: 5 (4 source + 1 test file)
- **Package Versions**: 0.1.0 (Alpha)
- **Python Target**: 3.11+
- **Build System**: hatchling
- **Dependency Manager**: uv

### LOC Breakdown

| File | LOC | Purpose |
|---|---|---|
| `middleware.py` | 347 | Core middleware class + hook implementations |
| `middleware_hooks.py` | 767 | Stateless hook logic (extractors, validators, trace spans) |
| `test_middleware.py` | 884 | Test suite with mocked governance client |
| `subagent_resolver.py` | 124 | Subagent detection + HITL conflict guards |
| `middleware_factory.py` | 74 | Factory function + config initialization |
| `__init__.py` | 92 | Public API surface + re-exports |

## File Structure

```
openbox_deepagent/
├── __init__.py              (92 LOC)   Public API
├── middleware.py            (347 LOC)  OpenBoxMiddleware class
├── middleware_factory.py    (74 LOC)   create_openbox_middleware()
├── middleware_hooks.py      (767 LOC)  Hook implementations
└── subagent_resolver.py     (124 LOC)  Subagent detection utilities

tests/
├── __init__.py              (0 LOC)
└── test_middleware.py       (884 LOC)  Full hook coverage + mocked governance

test-agent/                             Runnable example (requires API keys)
├── agent.py                            DeepAgents example
├── langgraph.json
├── pyproject.toml
├── SETUP.md
└── .env.example

Root:
├── pyproject.toml           (47 LOC)   Build config
├── CLAUDE.md                            AI assistant instructions
├── README.md                (721 LOC)  User documentation
└── LICENSE
```

## Module Responsibilities

### __init__.py (92 LOC)
**Public API surface.** Re-exports:
- From this package: `OpenBoxMiddleware`, `OpenBoxMiddlewareOptions`, `create_openbox_middleware`, `DEEPAGENT_BUILTIN_TOOLS`, `DEEPAGENT_SUBAGENT_TOOL`
- From `openbox_langgraph`: 15 items (errors, config, types, utilities)

**Key exports**: Users import everything from `openbox_deepagent` only, not submodules.

### middleware_factory.py (74 LOC)
**Single function: `create_openbox_middleware()`**
1. Validates keyword arguments
2. Calls `openbox_langgraph.config.initialize()` to validate API key + set global config
3. Constructs + returns `OpenBoxMiddleware` instance with resolved options

**Parameters**: 17 options (api_url, api_key, agent_name, known_subagents, etc.)

### middleware.py (347 LOC)
**Core class: `OpenBoxMiddleware`**
- Implements LangChain's `AgentMiddleware` interface
- 8 hooks (sync + async pairs): `before_agent`, `wrap_model_call`, `wrap_tool_call`, `after_agent`
- State per invocation: `_workflow_id`, `_run_id`, `_sync_mode`, `_pre_screen_response`
- Owns `GovernanceClient`, `WorkflowSpanProcessor`, options store
- Sync hooks delegate to async via `_run_async()` (thread pool when inside event loop)
- Public methods: `get_known_subagents()`, `astream_governed()`, `astream()`, `astream_events()`

**Key methods**:
- `before_agent()` → calls `handle_before_agent()`
- `wrap_model_call()` → PII redaction before model execution
- `wrap_tool_call()` → governance enforcement before/after tool
- `after_agent()` → cleanup + span processor finalization

### middleware_hooks.py (767 LOC)
**Stateless hook implementations** — all functions take middleware instance as first arg.

**Main entry points**:
- `handle_before_agent()` — SignalReceived → WorkflowStarted → LLMStarted pre-screen
- `handle_after_agent()` — WorkflowCompleted + SpanProcessor finalization
- `handle_wrap_model_call()` — PII redaction + model call execution
- `handle_wrap_tool_call()` — ToolStarted governance → tool execution → ToolCompleted

**Helpers**:
- `_extract_messages_from_state()` — Extract message list from agent state
- `_extract_message_content()` — Get first user message text
- `_resolve_tool_type()` — Map tool name to semantic type
- `_run_with_otel_context()` — Manual trace span creation across asyncio.Task boundaries
- `_create_activity_input()` — Build activity_input array with optional `__openbox` metadata
- Async governance evaluation: `evaluate_event()`, `evaluate_event_sync()`
- HITL polling: `_poll_until_decision()`, `_enforce_verdict()`

### subagent_resolver.py (124 LOC)
**Subagent detection + HITL conflict guards.**

**Main functions**:
- `resolve_subagent_from_tool_call()` — Extract `subagent_type` from `task` tool input
- `resolve_deepagent_subagent_name()` — Fallback for stream event path (legacy)
- `check_interrupt_on_conflict()` — Detect + raise if `interrupt_on` + OpenBox HITL both enabled

**Constants**:
- `DEEPAGENT_BUILTIN_TOOLS` = `{"task"}` — Tools not mapped to semantic types
- `DEEPAGENT_SUBAGENT_TOOL` = `"task"` — The tool dispatching subagents

## Dependency Graph

```
openbox_deepagent
├── openbox_langgraph (local editable)
│   ├── GovernanceClient
│   ├── GovernanceConfig
│   ├── WorkflowSpanProcessor
│   ├── enforce_verdict()
│   ├── poll_until_decision()
│   ├── Error types (5x)
│   └── LangChainGovernanceEvent
│
├── langchain-core ≥0.3.0
│   └── AgentMiddleware interface
│
└── langgraph ≥0.2.0
    ├── CompiledGraph
    ├── BaseMessage
    └── Event streaming
```

## Public API Surface

### Classes
- **`OpenBoxMiddleware`** — Middleware implementation; owns governance client + state
- **`OpenBoxMiddlewareOptions`** — TypedDict with 17 configuration fields

### Functions
- **`create_openbox_middleware()`** — Factory; returns OpenBoxMiddleware instance

### Constants
- **`DEEPAGENT_BUILTIN_TOOLS`** = `{"task"}`
- **`DEEPAGENT_SUBAGENT_TOOL`** = `"task"`

### Re-exported from openbox_langgraph
- **Errors**: 5 types (`GovernanceBlockedError`, `GovernanceHaltError`, etc.)
- **Types**: `GovernanceConfig`, `GovernanceVerdictResponse`, `Verdict`, etc.
- **Utilities**: `initialize()`, `get_global_config()`, `rfc3339_now()`, `safe_serialize()`
- **Classes**: `OpenBoxLangGraphHandler`, `OpenBoxLangGraphHandlerOptions`

## Governance Event Flow

```
┌─ invoke(state) / ainvoke(state)
│
├─ before_agent / abefore_agent
│  ├─ SignalReceived event
│  ├─ WorkflowStarted event
│  └─ LLMStarted pre-screen (guardrails PII check)
│     ↓ cache response in _pre_screen_response
│
├─ wrap_model_call / awrap_model_call
│  ├─ PII redaction (using pre-screen response)
│  ├─ Model invocation
│  └─ LLMCompleted event
│
├─ wrap_tool_call / awrap_tool_call
│  ├─ ToolStarted event → governance evaluation
│  ├─ Subagent detection (if task tool)
│  ├─ Activity classification (__openbox metadata)
│  ├─ Verdict enforcement (CONTINUE / BLOCK / REQUIRE_APPROVAL / HALT)
│  ├─ HITL polling (if REQUIRE_APPROVAL)
│  ├─ Tool execution
│  ├─ Trace span registration
│  └─ ToolCompleted event
│
└─ after_agent / aafter_agent
   ├─ WorkflowCompleted event
   └─ SpanProcessor finalization
```

## Testing Strategy

**File**: `tests/test_middleware.py` (884 LOC)

**Coverage**: Hook implementations, subagent detection, HITL polling, error handling

**Mocking**: All governance client calls + tracing components mocked via `unittest.mock`

**Framework**: `pytest-asyncio` with `asyncio_mode = "auto"`

**Test classes**:
- `TestBeforeAgent` — Pre-screen guardrails + WorkflowStarted
- `TestWrapModelCall` — PII redaction + LLMCompleted
- `TestWrapToolCall` — Tool governance + subagent detection
- `TestAfterAgent` — Session cleanup
- `TestSubagentDetection` — Subagent name extraction
- `TestHITLConflictGuard` — interrupt_on conflict detection

**No integration tests** — Requires real API keys + DeepAgents setup. See `test-agent/` for example.

## Code Patterns

### Naming Conventions

| Pattern | Usage | Example |
|---|---|---|
| `_run_*()` | Internal async runners | `_run_async()` |
| `_extract_*()` | Data extractors | `_extract_messages_from_state()` |
| `handle_*()` | Hook implementations | `handle_before_agent()` |
| `resolve_*()` | Detection/resolution | `resolve_subagent_from_tool_call()` |
| `check_*()` | Validation/guards | `check_interrupt_on_conflict()` |

### Import Organization

```python
# Standard library
import asyncio
from typing import TypedDict, Optional, Any

# Third-party (langchain, langgraph)
from langchain_core.messages import BaseMessage
from langgraph.constants import Send

# openbox-langgraph imports
from openbox_langgraph import GovernanceClient, enforce_verdict

# Local imports
from openbox_deepagent.subagent_resolver import resolve_subagent_from_tool_call
```

### Error Handling Patterns

```python
try:
    verdict = await client.evaluate_event(event)
except OpenBoxNetworkError:
    if on_api_error == "fail_closed":
        raise GovernanceBlockedError("Network error + fail_closed mode")
    # fail_open: continue normally
```

### Async/Sync Bridging

```python
def before_agent(self, state: AgentState) -> AgentState:
    """Sync hook delegates to async via _run_async()."""
    return self._run_async(handle_before_agent(self, state))

def _run_async(self, coro: Coroutine) -> Any:
    """Run async in thread pool if inside event loop, else use asyncio.run()."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No event loop → create one
        return asyncio.run(coro)
    # Inside event loop → use thread pool
    return loop.run_in_executor(None, lambda: asyncio.run(coro))
```

## Configuration Options (17 total)

### Required
- `api_url` (str) — OpenBox Core endpoint
- `api_key` (str) — API key (`obx_live_*` or `obx_test_*`)

### Optional Core
- `agent_name` (str) — Dashboard agent name (must match exactly)
- `known_subagents` (list[str]) — Expected subagent types
- `validate` (bool) — Validate API key on init (default: True)

### Optional Governance
- `on_api_error` (str) — "fail_open" or "fail_closed" (default: "fail_open")
- `governance_timeout` (float) — HTTP timeout in seconds (default: 30.0)
- `tool_type_map` (dict) — Custom semantic types
- `skip_chain_types` (set) — Middleware nodes to skip
- `skip_tool_types` (set) — Tools to skip entirely

### Optional HITL
- `hitl` (dict) — HITL config { enabled, poll_interval_ms, max_wait_ms, skip_tool_types }
- `guard_interrupt_on_conflict` (bool) — Raise if interrupt_on + HITL both enabled (default: True)

### Optional Events
- `send_chain_start_event` (bool) — Send WorkflowStarted (default: True)
- `send_chain_end_event` (bool) — Send WorkflowCompleted (default: True)
- `send_llm_start_event` (bool) — Send LLMStarted pre-screen (default: True)
- `send_llm_end_event` (bool) — Send LLMCompleted (default: True)

### Optional DB
- `sqlalchemy_engine` (Engine) — Existing engine to instrument (default: None)

## Quality Metrics

| Metric | Value | Notes |
|---|---|---|
| Test LOC | 884 | ~1.5x source LOC (2288) |
| Type hints | Strict mypy | All functions typed, no `# type: ignore` |
| Linting | ruff | E, F, I, UP, B, C4, PIE, RUF rules |
| Code style | ruff format | 100-char line length |
| Async support | Full | Both invoke() + ainvoke() paths |
| Trace integration | Manual | Span bridging across asyncio.Task boundaries |

---

**Last Updated**: 2026-03-21
**Version**: 0.1.0
