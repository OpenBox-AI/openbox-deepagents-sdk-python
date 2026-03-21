# System Architecture — openbox-deepagent

## High-Level Overview

```
┌────────────────────────────────────────────┐
│  Your DeepAgents Graph                    │
│  (create_deep_agent with middleware=[])   │
└────────────────┬───────────────────────────┘
                 │ ainvoke(state)
                 ▼
┌────────────────────────────────────────────┐
│  OpenBoxMiddleware (LangChain Hooks)       │
│                                            │
│  ┌── before_agent ──────────────────────┐ │
│  │ SignalReceived → WorkflowStarted     │ │
│  │ → LLMStarted pre-screen (guardrails) │ │
│  └────────────────────────────────────┘ │
│                                        │ │
│  ┌── wrap_model_call ────────────────┐ │ │
│  │ LLMStarted → PII redaction        │ │ │
│  │ → Model execution → LLMCompleted  │ │ │
│  └────────────────────────────────────┘ │
│                                        │ │
│  ┌── wrap_tool_call ────────────────┐  │ │
│  │ ToolStarted → Governance eval    │  │ │
│  │ → Subagent detect + classify     │  │ │
│  │ → HITL polling (if needed)       │  │ │
│  │ → Tool exec → ToolCompleted      │  │ │
│  └────────────────────────────────────┘ │
│                                        │ │
│  ┌── after_agent ───────────────────┐  │ │
│  │ WorkflowCompleted + Cleanup      │  │ │
│  └────────────────────────────────────┘ │
└────────────────┬───────────────────────────┘
                 │ Governance events
                 ▼
┌────────────────────────────────────────────┐
│  OpenBox Core (Policy Engine + HITL)       │
│  https://core.openbox.ai                   │
│                                            │
│  ├─ Rego Policy Evaluation (OPA)          │
│  ├─ Guardrails (PII detection, filters)   │
│  ├─ Activity Governance Engine (AGE)      │
│  └─ HITL Dashboard (approval/rejection)   │
└────────────────────────────────────────────┘
```

## Middleware Lifecycle

### Initialization
```python
middleware = create_openbox_middleware(
    api_url="https://core.openbox.ai",
    api_key="obx_live_...",
    agent_name="ResearchBot",
    known_subagents=["researcher", "writer"],
)
```

**Factory steps**:
1. Validate options (api_url, api_key, agent_name)
2. Call `openbox_langgraph.config.initialize()` to validate API key against Core
3. Create `GovernanceClient` (async httpx-based HTTP client)
4. Create `WorkflowSpanProcessor` (manages OTel spans)
5. Return `OpenBoxMiddleware` instance

**Per-invocation state** (reset on each ainvoke/invoke):
- `_workflow_id` — UUID for this governance session
- `_run_id` — UUID for this specific run
- `_sync_mode` — True if invoke() called (not ainvoke())
- `_pre_screen_response` — Cached first LLM guardrail response

## Governance Event Flow

### Event Sequence

```
┌─ ainvoke(state)
│
├─ abefore_agent(state)
│  │
│  ├─ Create SignalReceived event
│  │  └─ Send to OpenBox (logged, no decision)
│  │
│  ├─ Create WorkflowStarted event
│  │  └─ Send to OpenBox (logged, no decision)
│  │
│  ├─ Extract first user message
│  │
│  ├─ Create LLMStarted event (pre-screen)
│  │  ├─ Evaluate against guardrails
│  │  ├─ On PII: redact in-place, return modified message
│  │  ├─ On block: raise GuardrailsValidationError (graph never starts)
│  │  └─ Cache response in _pre_screen_response
│  │
│  └─ Return modified state
│
├─ awrap_model_call(model, messages)
│  │
│  ├─ On FIRST LLM call: reuse _pre_screen_response (no API call)
│  │
│  ├─ On SUBSEQUENT LLM calls:
│  │  ├─ Create new LLMStarted event
│  │  ├─ Evaluate against guardrails
│  │  └─ Apply PII redaction
│  │
│  ├─ Execute model
│  │
│  ├─ Create LLMCompleted event
│  │  └─ Send to OpenBox (logged, no decision)
│  │
│  └─ Return model output
│
├─ awrap_tool_call(tool_name, tool_input)
│  │
│  ├─ Create ToolStarted event
│  │
│  ├─ SUBAGENT DETECTION (if tool_name == "task")
│  │  ├─ Extract subagent_type from tool_input
│  │  ├─ Fall back to "general-purpose" if missing
│  │  └─ Log warning in DEBUG mode
│  │
│  ├─ TOOL CLASSIFICATION
│  │  ├─ Check tool_type_map
│  │  ├─ If subagent: use "a2a"
│  │  └─ Create __openbox metadata
│  │
│  ├─ BUILD activity_input
│  │  ├─ Original tool args [0] = tool_input dict
│  │  └─ Metadata [1] = {"__openbox": {tool_type, subagent_name}}
│  │
│  ├─ GOVERNANCE EVALUATION
│  │  ├─ Send ActivityStarted event to OpenBox
│  │  ├─ Policy engine evaluates
│  │  └─ Returns verdict: CONTINUE / BLOCK / REQUIRE_APPROVAL / HALT
│  │
│  ├─ VERDICT ENFORCEMENT
│  │  │
│  │  ├─ if CONTINUE:
│  │  │  └─ Proceed to tool execution
│  │  │
│  │  ├─ if BLOCK:
│  │  │  └─ Raise GovernanceBlockedError (tool does not execute)
│  │  │
│  │  ├─ if HALT:
│  │  │  └─ Raise GovernanceHaltError (session terminates)
│  │  │
│  │  └─ if REQUIRE_APPROVAL:
│  │     ├─ Enable HITL mode
│  │     ├─ Start while-true polling loop
│  │     │  ├─ Poll every poll_interval_ms
│  │     │  ├─ Check dashboard for approval/rejection
│  │     │  └─ Timeout after max_wait_ms
│  │     ├─ If approved: proceed to execution
│  │     ├─ If rejected: raise ApprovalRejectedError
│  │     └─ If timeout: raise ApprovalTimeoutError
│  │
│  ├─ TOOL EXECUTION
│  │  ├─ If OTel instrumented: create span (manual context attachment)
│  │  ├─ Execute tool function
│  │  ├─ Capture result/error
│  │  └─ Finalize span
│  │
│  ├─ CREATE ToolCompleted EVENT
│  │  └─ Send to OpenBox (Activity Governance Engine processes)
│  │
│  └─ Return tool result
│
└─ aafter_agent(state)
   │
   ├─ Create WorkflowCompleted event
   │  └─ Send to OpenBox (logged, no decision)
   │
   ├─ Finalize WorkflowSpanProcessor
   │  ├─ Flush pending spans
   │  └─ Clear per-invocation state
   │
   └─ Return final state
```

## Subagent Resolution

### How Subagents Are Detected

DeepAgents dispatches subagents via the `task` tool:
```python
task(description="Research AI", subagent_type="researcher")
```

**SDK detection process**:
1. `wrap_tool_call` hook fires with `tool_name="task"`, `tool_input={...}`
2. Extract `subagent_type` from tool_input
3. Embed as `__openbox.subagent_name` in governance event
4. Policies can target via: `input.activity_input[1]["__openbox"].subagent_name == "researcher"`

### Subagent Metadata Structure

**Generated by SDK**:
```json
{
  "activity_input": [
    {"description": "...", "subagent_type": "researcher"},
    {"__openbox": {
      "tool_type": "a2a",
      "subagent_name": "researcher"
    }}
  ]
}
```

**Rego policy example**:
```rego
# Require approval for researcher subagent tasks
result := {"decision": "REQUIRE_APPROVAL", "reason": "Researcher tasks need review."} if {
    input.event_type == "ActivityStarted"
    input.activity_type == "task"
    not input.hook_trigger
    some item in input.activity_input
    meta := item["__openbox"]
    meta.subagent_name == "researcher"
}
```

### Fallback Behavior

If `subagent_type` is missing from `task` input:
1. Fall back to `"general-purpose"`
2. Log warning: `task tool input missing subagent_type`
3. Emit event with `subagent_name: "general-purpose"`

This prevents crashes but may mask configuration errors. Check DEBUG logs to diagnose.

## Pre-screen Optimization

### Problem

Each `ainvoke()` may call the model multiple times. Without optimization, every LLM call would trigger a separate governance round-trip (5 API calls → 5 governance evaluations).

### Solution

Cache first LLM call's guardrail response and reuse it:

```
abefore_agent: Create LLMStarted #1 → Evaluate → Cache response
awrap_model_call (1st call): Reuse cached response (NO API CALL)
awrap_model_call (2nd call): Create new LLMStarted #2 → Evaluate → Return response
awrap_model_call (3rd call): Create new LLMStarted #3 → Evaluate → Return response
...
```

**State management**:
```python
class OpenBoxMiddleware:
    _pre_screen_response: GovernanceVerdictResponse | None = None

    async def abefore_agent(self, state):
        # Evaluate pre-screen guardrails once
        self._pre_screen_response = await client.evaluate_event(llm_started_event)

    async def awrap_model_call(self, model, messages):
        if is_first_call and self._pre_screen_response:
            verdict = self._pre_screen_response  # Reuse
        else:
            verdict = await client.evaluate_event(llm_started_event)  # New eval
```

## Tool Classification

### Built-in Types

| Type | Meaning | Examples |
|---|---|---|
| `"http"` | HTTP/HTTPS calls | `search_web`, `fetch_url`, `api_call` |
| `"database"` | SQL/NoSQL queries | `query_db`, `execute_sql`, `mongodb_find` |
| `"builtin"` | Language/framework builtins | `code_interpreter`, `math_eval` |
| `"a2a"` | Agent-to-agent dispatch | `task` tool (auto-classified) |

### Custom Mapping

Pass `tool_type_map` to override:
```python
middleware = create_openbox_middleware(
    tool_type_map={
        "search_web": "http",
        "query_db": "database",
        "send_email": "http",  # Custom classification
    }
)
```

### Metadata Injection

For each tool call, SDK appends `__openbox` to `activity_input`:

```json
{
  "activity_input": [
    {"query": "SELECT * FROM users"},  // Original tool args
    {"__openbox": {"tool_type": "database"}}
  ]
}
```

**No Core changes needed** — OPA sees it as part of activity_input naturally.

## Sync/Async Bridging

### The Dual Interface Problem

LangChain `AgentMiddleware` has both sync and async hooks:
```python
def before_agent(self, state: AgentState) -> AgentState:  # Sync
    ...

async def abefore_agent(self, state: AgentState) -> AgentState:  # Async
```

The SDK must support both `invoke()` and `ainvoke()`:
```python
await agent.ainvoke(state)  # Async path
agent.invoke(state)          # Sync path
```

### Solution: Thread Pool Delegation

```python
def before_agent(self, state: AgentState) -> AgentState:
    """Sync hook delegates to async via thread pool."""
    return self._run_async(self.abefore_agent(state))

def _run_async(self, coro: Coroutine) -> Any:
    """Run async in thread pool if inside event loop, else use asyncio.run()."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No event loop running → safe to create one
        return asyncio.run(coro)

    # Inside event loop (e.g., from ainvoke) → use thread pool
    # This avoids "asyncio.run() cannot be called from a running event loop" error
    return loop.run_in_executor(None, lambda: asyncio.run(coro))
```

### Sync Mode Flag

When `invoke()` is called directly, `_sync_mode=True`. This switches governance calls to use sync `httpx.Client`:

```python
if self._sync_mode:
    # Use sync HTTP client
    verdict = self.client.evaluate_event_sync(event)
else:
    # Use async HTTP client
    verdict = await self.client.evaluate_event(event)
```

This prevents context cancellation from `asyncio.run()` teardown.

## OTel Span Bridging

### The Context Break Problem

LangGraph spawns asyncio.Tasks for tool/LLM execution, breaking OTel trace context:

```python
# Inside wrap_tool_call (has trace context A)
task = asyncio.create_task(tool_func())  # ← Task created without context A
await task  # ← Context A lost in Task execution
# Trace context is broken
```

### Solution: Manual Span Creation

```python
async def _run_with_otel_context(
    context: trace.Context,
    task: Coroutine,
) -> Any:
    """Execute task with OTel context attached."""
    # Manually create span in current context
    span = tracer.start_span(name)

    # Attach context to task execution
    with trace.use_span(span):
        result = await task

    # Register span with WorkflowSpanProcessor
    processor.register_span(span)

    return result
```

**Implementation in middleware**:
1. `wrap_tool_call` creates span BEFORE tool execution
2. Task executes with span context
3. `WorkflowSpanProcessor` collects spans per invocation
4. `after_agent` finalizes processor (flush + cleanup)

## HITL Polling

### Approval Workflow

```
Tool execution blocked by REQUIRE_APPROVAL
│
├─ Start polling loop (while True)
│
├─ Poll OpenBox every poll_interval_ms
│  └─ GET /decisions/{workflow_id}
│
├─ Loop until:
│  ├─ Human approves → Proceed to execution
│  ├─ Human rejects → Raise ApprovalRejectedError
│  └─ Timeout exceeds max_wait_ms → Raise ApprovalTimeoutError
│
└─ Finalize approval state
```

### Implementation

```python
async def _poll_until_decision(
    client: GovernanceClient,
    workflow_id: str,
    poll_interval_ms: int,
    max_wait_ms: int,
) -> ApprovalDecision:
    """Poll for human approval in dashboard."""
    elapsed = 0

    while elapsed < max_wait_ms:
        decision = await client.get_approval_decision(workflow_id)

        if decision.status == "approved":
            return decision
        elif decision.status == "rejected":
            raise ApprovalRejectedError(f"Decision rejected: {decision.reason}")

        await asyncio.sleep(poll_interval_ms / 1000)
        elapsed += poll_interval_ms

    raise ApprovalTimeoutError(f"Approval timeout after {max_wait_ms}ms")
```

### HITL + interrupt_on Conflict

DeepAgents has its own interrupt mechanism via `interrupt_on`. Using both simultaneously causes:
- Double-pausing (OTel hook HITL + DeepAgents interrupt_on both pause)
- Unpredictable execution order
- Confusion in dashboard state

**SDK guard** (raises at startup):
```python
def check_interrupt_on_conflict(graph: CompiledGraph) -> bool:
    """Detect if interrupt_on is configured on the graph."""
    if hasattr(graph, 'interrupt_on') and graph.interrupt_on:
        raise ValueError(
            "[OpenBox] DeepAgents graph has interrupt_on configured "
            "AND OpenBox HITL is enabled. These conflict — OpenBox must own the HITL flow. "
            "Remove interrupt_on from create_deep_agent, or set "
            "guard_interrupt_on_conflict=False to suppress this check."
        )
```

## Error Handling Flow

### Error Types & Handling

```
governance evaluation failure
│
├─ GovernanceBlockedError (policy returned BLOCK)
│  └─ Tool does NOT execute
│
├─ GovernanceHaltError (policy returned HALT)
│  └─ Session terminates, propagate immediately
│
├─ GuardrailsValidationError (guardrail blocked prompt)
│  └─ Graph never starts (fails in before_agent)
│
├─ ApprovalRejectedError (human rejected REQUIRE_APPROVAL)
│  └─ Tool does NOT execute, converted to GovernanceHaltError
│
├─ ApprovalTimeoutError (HITL polling exceeded max_wait_ms)
│  └─ Tool does NOT execute, converted to GovernanceHaltError
│
├─ OpenBoxNetworkError (API unreachable)
│  ├─ if on_api_error="fail_open": continue silently
│  └─ if on_api_error="fail_closed": raise GovernanceBlockedError
│
└─ OpenBoxAuthError (invalid API key)
   └─ Raised at initialization; never suppressed
```

### User-Facing Handling

```python
try:
    result = await agent.ainvoke({"messages": [...]})
except GovernanceBlockedError as e:
    # Policy returned BLOCK
    print(f"Blocked: {e}")
except ApprovalTimeoutError as e:
    # HITL approval timed out
    print(f"Approval timeout: {e}")
except GuardrailsValidationError as e:
    # Guardrail blocked the prompt before graph started
    print(f"Guardrail violation: {e}")
```

## Performance & Scalability

### Per-Invocation Overhead

**Typical governance latency per invocation** (assuming 30ms API latency):
- Pre-screen LLMStarted: 30ms (cached for first model call)
- Per tool call: 30ms (governance) + tool exec time
- Per additional LLM call: 30ms (governance) + inference time
- WorkflowCompleted: 30ms

**Total overhead**: ~60ms per tool call + LLM call pair (low relative to model inference).

### Optimization Techniques

1. **Pre-screen caching** — Avoid duplicate guardrail evaluation on first LLM call
2. **Skip chain types** — Skip governance for DeepAgents middleware nodes (model, tools, etc.)
3. **Skip tool types** — Exclude low-risk tools from governance entirely
4. **Fail-open default** — Don't block on network errors (set fail_closed if needed)

## Debugging & Observability

### Debug Mode

```bash
OPENBOX_DEBUG=1 python agent.py
```

**Output streams**:
1. `[OBX_EVENT]` (stderr) — Raw LangGraph events logged as they fire
2. `[OpenBox Debug]` (stdout) — Governance requests and policy responses

### Common Issues & Diagnostics

| Issue | Check |
|---|---|
| Policies never fire | `workflow_type` matches dashboard agent name |
| Subagent targeting doesn't work | `subagent_name` in `__openbox` metadata (check DEBUG logs) |
| Rule fires twice per tool | Missing `not input.hook_trigger` guard in Rego |
| HITL never prompts | `hitl.enabled=True` and policy returns `REQUIRE_APPROVAL` |
| Sync mode hangs | Ensure `invoke()` not called inside async context |

---

**Last Updated**: 2026-03-21
**Version**: 0.1.0
