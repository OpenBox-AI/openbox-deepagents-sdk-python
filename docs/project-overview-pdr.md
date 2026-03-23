# OpenBox DeepAgents SDK — Project Overview & PDR

## Project Overview

**openbox-deepagent** v0.1.0 — Python governance SDK extending OpenBox's core governance layer for DeepAgents applications. Adds real-time policy enforcement, guardrails, human-in-the-loop (HITL) approval, and semantic tool classification via LangChain's `AgentMiddleware` interface.

### What It Solves

DeepAgents dispatches subagents through the built-in `task` tool. These execute *inside* the tool body, making their internal events invisible to outer LangGraph streams. `openbox-deepagent` makes subagent governance transparent by:

1. **Per-subagent policy targeting** — Govern `researcher` subagent differently from `writer`
2. **Subagent type extraction** — Automatically detect `subagent_type` from `task` args and embed as `__openbox` metadata
3. **HITL conflict prevention** — Detect and prevent clashes with DeepAgents' native `interrupt_on`
4. **Built-in tool classification** — Automatic `"a2a"` (agent-to-agent) classification for subagent dispatches

### Value Proposition vs openbox-langgraph-sdk

- **Base SDK** (`openbox-langgraph-sdk`): Generic LangGraph handler + governance events + OPA policies
- **This SDK** (`openbox-deepagent`): DeepAgents-specific middleware + subagent resolution + pre-screen optimization + sync/async bridging for the `invoke()`/`ainvoke()` dual interface

### Target Audience

- DeepAgents developers building agentic applications needing governance
- Teams enforcing guardrails, HITL approval flows, and policy-based tool access control
- Organizations requiring audit trails and per-component compliance

### Current Status

**v0.1.0 (Alpha)** — Feature-complete with known limitations documented in README. Primary limitation: subagent internals remain opaque to governance (only `task` dispatch itself is governed, not tools called inside subagents).

## Product Development Requirements

### Functional Requirements

| Requirement | Status | Details |
|---|---|---|
| Middleware implementation | ✓ Complete | LangChain `AgentMiddleware` with sync + async hooks |
| Policy enforcement | ✓ Complete | Rego-based OPA policies via OpenBox Core |
| Guardrails (PII redaction) | ✓ Complete | First LLM call guardrail pre-screen before graph invocation |
| HITL approval workflows | ✓ Complete | Dashboard polling for `REQUIRE_APPROVAL` verdicts with configurable timeouts |
| Subagent detection | ✓ Complete | Extract `subagent_type` from `task` tool args and embed in governance event |
| Tool classification | ✓ Complete | Semantic types (http, database, builtin, a2a) with custom mapping |
| Behavior Rules (AGE) | ✓ Complete | Activity Governance Engine pattern matching (inherited from langgraph SDK) |
| Database governance | ✓ Complete | SQLAlchemy instrumentation for retroactive DB engine governance |
| Debug mode | ✓ Complete | `OPENBOX_DEBUG=1` logs all events and governance decisions |

### Non-Functional Requirements

| Requirement | Status | Details |
|---|---|---|
| Python 3.11+ support | ✓ Complete | Minimal typing, native async syntax |
| Async-first design | ✓ Complete | Primary path via `ainvoke()` with sync fallback via `invoke()` |
| OTel instrumentation | ✓ Complete | Manual span bridging across asyncio.Task boundaries |
| Error handling | ✓ Complete | 5 governance error types + fallback modes (fail-open/fail-closed) |
| Test coverage | ✓ Complete | 884 LOC test suite covering middleware hooks, subagent detection, HITL |
| Performance | ✓ In scope | Pre-screen optimization caches first LLM call response to avoid duplicate governance |
| Zero-config setup | ~ Partial | API key validation required; subagent list must be pre-declared |

### Acceptance Criteria

- Governance events fire before tool execution (ActivityStarted) and after completion (ActivityCompleted)
- Subagent policies can target specific subagent types via `__openbox.subagent_name`
- HITL polling works without blocking event loop (thread pool executor)
- Sync mode (`invoke()`) avoids asyncio context cancellation from `asyncio.run()` teardown
- All governance errors bubble up with descriptive messages
- Debug logs are complete and actionable (workflow_type matches dashboard, subagent_name resolved, no double-triggers on http spans)

### Success Metrics

- **Adoption**: Available on PyPI, integrated into DeepAgents documentation
- **Stability**: Zero governance-layer issues in production (errors are policy-driven, not SDK bugs)
- **Developer experience**: Setup in 5 minutes, debug session in 2 minutes
- **Test coverage**: >90% of hook paths covered by unit tests

## Technical Constraints & Dependencies

### Dependencies

| Package | Version | Purpose | Status |
|---|---|---|---|
| `openbox-langgraph-sdk` | ≥0.1.0 | Base governance layer (local editable) | Active |
| `langchain-core` | ≥0.3.0 | AgentMiddleware interface | Active |
| `langgraph` | ≥0.2.0 | Graph execution + event streams | Active |
| `deepagents` (optional) | ≥0.1.0 | Type hints only (not required at runtime) | Optional |

### Architectural Constraints

1. **Subagent opaqueness** — DeepAgents executes subagents *inside* `task` tool body. Cannot govern subagent-internal tool calls. Workaround: duplicate high-risk tools in outer agent.

2. **HTTP span context** — `httpx` instrumentation captures outer agent tool HTTP calls only. Subagent HTTP calls in separate async context are not captured.

3. **Behavior Rules scope** — AGE tracks patterns **within single `ainvoke()` call only**. Cross-turn pattern detection not supported.

4. **Event loop bridging** — LangGraph spawns asyncio.Tasks that break OTel trace context. SDK must manually create spans and register with `WorkflowSpanProcessor`.

5. **Sync/async duality** — `invoke()` uses sync `httpx.Client` to avoid asyncio.run() teardown issues; `ainvoke()` uses async client.

## Implementation Guidance

### Architecture Layers

```
┌─────────────────────────────────────────┐
│  User Graph (create_deep_agent)         │
├─────────────────────────────────────────┤
│  OpenBoxMiddleware (4 hooks)            │
│  ├─ before_agent (pre-screen guardrails)│
│  ├─ wrap_model_call (PII redaction)    │
│  ├─ wrap_tool_call (governance events)  │
│  └─ after_agent (session cleanup)       │
├─────────────────────────────────────────┤
│  OpenBox Core (Policy Engine + HITL)    │
└─────────────────────────────────────────┘
```

### Key Design Decisions

1. **Pre-screen optimization**: Cache first LLM's guardrail response in `abefore_agent`, reuse in `awrap_model_call` to eliminate duplicate API call.

2. **Middleware over handler**: Use LangChain's middleware hook interface instead of wrapping graph with `astream_events`. Integrates seamlessly with DeepAgents' own middleware stack.

3. **Subagent metadata embedding**: Append `__openbox` sentinel object to `activity_input`. OPA sees it natively without Core schema changes.

4. **Manual OTel span bridging**: LangGraph's asyncio.Task execution breaks trace context. SDK manually creates spans, stores trace_id in WorkflowSpanProcessor, re-attaches on completion.

5. **Fail-open default**: Governance errors do not block execution by default. Set `on_api_error="fail_closed"` to block on network/auth failures.

### Code Organization

- **middleware_factory.py** (74 LOC) — Entry point, config validation
- **middleware.py** (347 LOC) — Hook implementations + state management
- **middleware_hooks.py** (767 LOC) — Stateless hook logic (extractors, validators, span management)
- **subagent_resolver.py** (124 LOC) — Subagent detection + HITL conflict guards
- **tests/test_middleware.py** (884 LOC) — Full hook coverage with mocked governance client

## Versioning & Release Policy

### Current Version: 0.1.0 (Alpha)

- Feature-complete for initial DeepAgents support
- Known limitations documented (subagent internals opaque, Behavior Rules scope)
- Breaking changes possible before 1.0.0

### Semver Rules

- **0.x.y**: Major breaking changes allowed
- **1.x.y**: Stable API; breaking changes require 2.0.0
- **Patch releases**: Bug fixes, no API changes

## Roadmap (Candidate Improvements)

### Post v0.1.0

1. **Subagent internal governance** — If DeepAgents adds event streaming for subagent internals, extend middleware to govern subagent-internal tool calls
2. **Cross-turn Behavior Rules** — Extend AGE to track patterns across multiple `ainvoke()` calls using `thread_id` correlation
3. **Explicit subagent policies** — Alternative syntax to target subagents (vs. embedding `__openbox` metadata)
4. **Health checks** — Periodic connectivity validation to OpenBox Core with exponential backoff
5. **Performance profiling** — Hook overhead measurement and latency tracking
6. **Integration tests** — Live DeepAgents agent tests (currently mocked governance client)

## Known Open Questions

1. **Subagent framework evolution** — Will DeepAgents add tighter introspection into subagent execution in future versions?
2. **Policy versioning** — Should SDK cache policy definitions locally for offline evaluation?
3. **Multi-region support** — Is there a use case for multiple OpenBox Core regions per agent?
4. **Custom verdict types** — Should customers be able to define custom verdicts beyond CONTINUE/BLOCK/REQUIRE_APPROVAL/HALT?

---

**Last Updated**: 2026-03-21
**Version**: 0.1.0
**Maintainer**: OpenBox AI
