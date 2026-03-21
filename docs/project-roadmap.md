# Project Roadmap — openbox-deepagent

## Current Status: v0.1.0 (Alpha)

**Release Date**: 2026-03 | **Stability**: Alpha | **Breaking Changes**: Expected before 1.0.0

### Phase 1: Foundation (✓ Complete)

Core DeepAgents governance via LangChain middleware interface.

| Feature | Status | Details |
|---|---|---|
| AgentMiddleware implementation | ✓ | 8 hooks (sync + async pairs) |
| Policy enforcement (Rego/OPA) | ✓ | Full OpenBox Core integration |
| Guardrails (PII + content filter) | ✓ | Pre-screen on first LLM call |
| HITL approval workflows | ✓ | Dashboard polling + configurable timeouts |
| Subagent detection | ✓ | Extract subagent_type from task input |
| Tool classification | ✓ | Semantic types (http, database, builtin, a2a) |
| Behavior Rules (AGE) | ✓ | Activity Governance Engine pattern matching |
| Database governance | ✓ | SQLAlchemy instrumentation + retroactive injection |
| Sync mode support | ✓ | invoke() without asyncio.run() context issues |
| Test coverage | ✓ | 884 LOC comprehensive suite |
| Debug mode | ✓ | OPENBOX_DEBUG=1 logging |
| Documentation | ✓ | User-facing README (721 LOC) |

## Known Limitations (v0.1.0)

### 1. Subagent Internals Opaque
**Status**: By design (DeepAgents constraint)

Subagents execute inside the `task` tool body. Their internal tool calls and LLM calls are not surfaced in the outer event stream. Only the `task` dispatch itself is governed.

**Impact**:
- Cannot govern `search_web` calls inside the `researcher` subagent
- Cannot apply per-tool rate-limiting to subagent-internal calls
- Can only govern the dispatch decision (BLOCK/REQUIRE_APPROVAL)

**Workaround**: Add high-risk tools to both outer agent and subagent tool lists for full governance.

**Future**: If DeepAgents adds event streaming for subagent internals, this can be lifted.

### 2. Behavior Rules (AGE) Scope Limited
**Status**: By design (session scope)

AGE tracks patterns **within a single ainvoke() call only**. Cross-turn pattern detection is not yet supported.

**Impact**:
- Can rate-limit "task dispatches per invocation" but not "per session"
- Cannot detect "called the writer subagent more than 2 times across the entire conversation"

**Future**: Extend AGE to track patterns across multiple invocations using `thread_id` correlation.

### 3. HTTP Spans Captured for Outer Agent Tools Only
**Status**: By design (asyncio context)

The httpx instrumentation captures calls made during outer agent tool execution. HTTP calls inside subagent tool bodies run in a separate async context and are not captured as spans on the task `ActivityCompleted`.

**Impact**:
- Behavior Rules cannot match on HTTP patterns inside subagents
- Audit logs show task dispatch but not the HTTP requests it made

**Future**: Improve asyncio context propagation to capture subagent HTTP spans.

### 4. Zero-Config Setup Not Fully Supported
**Status**: Partial

Users must pre-declare `known_subagents` at middleware creation time. If a new subagent type is added later, the middleware must be recreated.

**Impact**:
- Dynamic subagent discovery not supported
- Requires explicit list in initialization

**Future**: Add runtime subagent registration via API endpoint.

### 5. OPA Policy Versioning Not Built-in
**Status**: Out of scope

No local caching or offline evaluation of policies. Policies are always fetched from OpenBox Core.

**Impact**:
- Offline governance not supported
- Policy updates apply immediately (no gradual rollout)

**Future**: Consider local policy caching with TTL for offline resilience.

## Roadmap: v0.2.0 (Candidate)

**Estimated Timeline**: 2026-06 | **Focus**: Subagent observability & cross-turn patterns

### Planned Features

#### 1. Subagent Internal Instrumentation (if DeepAgents supports event streaming)
**Priority**: High | **Effort**: 3 sprints | **Dependency**: DeepAgents framework changes

If DeepAgents adds event streaming for subagent-internal tool/LLM calls:
- Extend middleware to govern subagent-internal events
- Apply per-subagent tool restrictions
- Track subagent-internal HTTP calls as spans

**Success metric**: Policies can target `researcher` subagent's `search_web` tool specifically.

#### 2. Cross-Turn Behavior Rules (AGE Extension)
**Priority**: Medium | **Effort**: 2 sprints | **Dependency**: OpenBox Core support

Extend AGE to track patterns across multiple `ainvoke()` calls using `thread_id` correlation:
- Rate-limit subagent dispatches per session (not per invocation)
- Detect repeated suspicious patterns across turns
- Time-series analysis (e.g., "high activity between 9-10 AM UTC")

**Success metric**: Policy can block if "writer subagent called more than 5 times in this session".

#### 3. Dynamic Subagent Registration
**Priority**: Low | **Effort**: 1 sprint | **Dependency**: OpenBox Core API

Add runtime subagent registration instead of pre-declaration:
```python
middleware.register_subagent("new-subagent")
```

**Success metric**: Subagents can be added/removed without middleware recreation.

#### 4. Health Checks & Resilience
**Priority**: Medium | **Effort**: 2 sprints

- Periodic connectivity validation to OpenBox Core
- Exponential backoff on transient network errors
- Circuit breaker pattern for fail-closed mode
- Metrics export (latency, error rates, policy decisions)

**Success metric**: Middleware detects and recovers from network partitions within 30 seconds.

### Breaking Changes (Expected)

- `OpenBoxMiddlewareOptions` may gain new required fields
- Error hierarchy may be restructured for clarity
- Config format may change to support new features

## Roadmap: v1.0.0 (Stable API)

**Estimated Timeline**: 2026-09 | **Focus**: API stability, production readiness

### Commitment

- Stable public API (no breaking changes without 2.0.0)
- Full subagent governance (if DeepAgents adds support)
- Cross-turn Behavior Rules
- Comprehensive integration tests with real DeepAgents graphs
- Performance SLOs (governance overhead <100ms per invocation)
- Production runbooks and troubleshooting guides

### Criteria for Release

1. ✓ All v0.2.0 planned features implemented
2. ✓ Zero critical bugs in production use (if any)
3. ✓ Full documentation (user guide + API reference + architecture)
4. ✓ Integration tests passing with real DeepAgents/OpenBox Core
5. ✓ Performance profiling complete (latency, memory, CPU)
6. ✓ Security audit complete

## Vision: v2.0.0+

**Timeline**: 2027+ | **Focus**: Multi-SDK convergence, enterprise features

### Long-term Goals

1. **Multi-SDK Parity**
   - Align DeepAgents, LangGraph, and Temporal SDKs on governance semantics
   - Single policy language for all frameworks
   - Unified audit logs across SDKs

2. **Advanced Observability**
   - Real-time governance dashboard (not just HITL)
   - Policy effectiveness analytics
   - Cost tracking per governance decision

3. **Declarative Governance**
   - Alternative to Rego for simple use cases (DSL or YAML)
   - Policy templates (rate-limiting, geofencing, cost limits)
   - Versioned policy rollout with canary deployment

4. **Tight DeepAgents Integration**
   - Built-in as optional middleware in create_deep_agent()
   - Automatic subagent type detection
   - Unified HITL with DeepAgents interrupts

5. **Performance at Scale**
   - Governance caching strategies (policy local evaluation)
   - Batch evaluation for multi-agent scenarios
   - Sub-10ms governance overhead target

## Open Questions & Decisions Needed

### 1. Subagent Framework Evolution
**Question**: Will DeepAgents add tighter introspection into subagent execution?

**Options**:
- A) Event streaming for subagent-internal calls (ideal for governance)
- B) Subagent state introspection API (alternative)
- C) No change (current limitation persists)

**Recommendation**: Engage DeepAgents team early to prioritize this.

### 2. Policy Versioning Strategy
**Question**: Should SDKs cache and evaluate policies locally for offline resilience?

**Options**:
- A) Always fetch from Core (current, simple, requires connectivity)
- B) Local cache with TTL (hybrid, adds complexity)
- C) Offline-first evaluation (complex, requires schema versioning)

**Recommendation**: Defer to v0.2.0 if offline governance becomes a use case.

### 3. Custom Verdict Types
**Question**: Should customers define custom verdicts beyond CONTINUE/BLOCK/REQUIRE_APPROVAL/HALT?

**Options**:
- A) No (current, keep semantics simple)
- B) Yes, via plugin interface (extensible, adds complexity)
- C) Yes, via Core config (centralized, no code changes)

**Recommendation**: Monitor user feedback; defer unless strong demand.

### 4. Multi-Region Support
**Question**: Is there a use case for routing to multiple OpenBox Core regions per agent?

**Options**:
- A) No (current, single api_url)
- B) Yes, via failover strategy (automatic region selection)
- C) Yes, via policy-based routing (route based on activity type)

**Recommendation**: Not in roadmap unless customer request.

## Development Phases (Detailed)

### Current Phase: v0.1.0 (Stability Focus)
- **Status**: In production alpha
- **Goals**: Fix bugs, gather feedback, refine based on real usage
- **Cadence**: Patch releases as needed (0.1.1, 0.1.2, etc.)
- **Timeline**: Now → 2026-05

### Next Phase: v0.2.0 (Feature Expansion)
- **Status**: Planning → Development (2026-04)
- **Goals**: Address known limitations, add subagent observability
- **Cadence**: Point releases (0.2.0, 0.2.1, etc.)
- **Timeline**: 2026-04 → 2026-08

### Final Phase: v1.0.0 (API Stability)
- **Status**: Post-release stability (2026-09+)
- **Goals**: Frozen public API, production SLOs
- **Cadence**: Major version only (1.0.0, 1.1.0, etc.)
- **Timeline**: 2026-09 → indefinite

## How to Contribute

1. **Report bugs** — Open GitHub issue with reproduction steps
2. **Request features** — Discuss in GitHub discussions before opening issue
3. **Submit PRs** — See CONTRIBUTING.md (forthcoming)
4. **Improve docs** — Typos, clarifications, examples always welcome

## Related Projects

- **[openbox-langgraph-sdk](../openbox-langgraph-sdk-python)** — Base governance SDK for LangGraph (parent dependency)
- **[OpenBox Dashboard](https://dashboard.openbox.ai)** — Policy editor + HITL UI + audit logs
- **[OpenBox Core](https://core.openbox.ai)** — Policy engine + Behavior Rules

---

**Last Updated**: 2026-03-21
**Version**: 0.1.0
**Maintainer**: OpenBox AI
