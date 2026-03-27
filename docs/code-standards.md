# Code Standards — openbox-deepagent

## Language & Version Requirements

- **Python**: 3.11+ (use modern async/typing syntax)
- **No Python 3.10 or earlier support**
- Use type hints everywhere (strict mypy mode enforced)

## Style & Formatting

### Linting (Ruff)

**Line length**: 100 characters (enforced)

**Selected rules**:
- `E` — pycodestyle errors
- `F` — Pyflakes errors
- `I` — isort (import sorting)
- `UP` — pyupgrade (modern Python syntax)
- `B` — flake8-bugbear (common bugs)
- `C4` — flake8-comprehensions (list/dict comprehensions)
- `PIE` — flake8-pie (Pythonic idioms)
- `RUF` — Ruff-specific rules

**Formatting**:
```bash
# Check formatting
uv run ruff check openbox_deepagent/ tests/

# Auto-fix issues
uv run ruff check --fix openbox_deepagent/ tests/

# Format code
uv run ruff format openbox_deepagent/ tests/
```

### Type Checking (mypy)

**Strict mode enabled**:
```ini
[tool.mypy]
python_version = "3.11"
strict = true
```

This means:
- All functions must have type hints (no untyped `def`)
- No implicit `Any` type
- All imports must be resolvable
- Run before committing:
  ```bash
  uv run mypy openbox_deepagent/
  ```

## Import Organization

**Order** (top to bottom):
1. Standard library (`asyncio`, `typing`, etc.)
2. Third-party (`langchain`, `langgraph`, etc.)
3. openbox-langgraph imports
4. Local imports

**Example**:
```python
import asyncio
from typing import Optional, Any, Coroutine

from langchain_core.messages import BaseMessage
from langgraph.constants import Send

from openbox_langgraph import GovernanceClient, enforce_verdict

from openbox_deepagent.subagent_resolver import resolve_subagent_from_tool_call
```

**Rules**:
- One blank line between groups
- Use `from X import Y` over `import X`; `import X as Y` only when necessary
- Avoid relative imports; use absolute paths

## Naming Conventions

### Files
- **kebab-case** with descriptive names
- Examples: `middleware_factory.py`, `subagent_resolver.py`, `middleware_hooks.py`
- Test files: `test_middleware.py` (mirror source structure)

### Functions
- **snake_case**
- **Prefixes indicate intent**:
  - `_run_*()` — Async runners (private, internal control flow)
  - `_extract_*()` — Data extraction helpers
  - `handle_*()` — Hook implementations (stateless functions taking middleware as first arg)
  - `resolve_*()` — Detection/resolution logic
  - `check_*()` — Validation guards

**Examples**:
```python
def _run_async(coro: Coroutine) -> Any: ...
def _extract_messages_from_state(state: AgentState) -> list[BaseMessage]: ...
async def handle_before_agent(mw: OpenBoxMiddleware, state: AgentState) -> AgentState: ...
def resolve_subagent_from_tool_call(tool_input: Any) -> str: ...
def check_interrupt_on_conflict(graph: CompiledGraph) -> bool: ...
```

### Classes
- **PascalCase**
- Prefix with intent if generic:
  - Example: `OpenBoxMiddleware` (not `Middleware`)

### Constants
- **UPPER_CASE_WITH_UNDERSCORES**
- Examples: `DEEPAGENT_BUILTIN_TOOLS`, `DEEPAGENT_SUBAGENT_TOOL`

### Private/Internal
- Prefix with single underscore: `_internal_var`, `_private_method()`
- Do NOT use double underscore (`__`) name mangling

## Documentation & Comments

### Docstrings (All Public Functions/Classes)

**Style**: Google-style docstrings (brief, then sections)

**Template**:
```python
async def handle_before_agent(
    middleware: OpenBoxMiddleware,
    state: AgentState,
) -> AgentState:
    """Initialize workflow and run pre-screen guardrails.

    Pre-screens the first user message for PII/content policy violations
    before the graph begins execution. Caches response in
    middleware._pre_screen_response to avoid duplicate governance round-trip
    in wrap_model_call.

    Args:
        middleware: Middleware instance owning governance client.
        state: Agent state from LangChain hook.

    Returns:
        Modified state with SignalReceived/WorkflowStarted/LLMStarted events
        already evaluated.

    Raises:
        GuardrailsValidationError: If guardrail blocked the prompt.
        GovernanceHaltError: If policy returned HALT.
    """
```

**Sections** (as needed):
- Brief (1 line)
- Extended description (2-3 sentences)
- Args (if any)
- Returns (if any)
- Raises (if any)
- Examples (rarely, for public API only)

### Inline Comments

**Use sparingly.** Code should be self-documenting via naming + type hints.

**When to use**:
- Non-obvious algorithmic logic (e.g., trace span bridging)
- Workarounds for framework quirks (e.g., asyncio.run() teardown issues)
- Performance-critical sections

**Format**: `# Comment explaining WHY, not WHAT`

**Example** (good):
```python
# LangGraph spawns asyncio.Tasks that break trace context.
# Manually create spans and register with WorkflowSpanProcessor to maintain traceability.
span = tracer.start_span(name)
```

**Example** (bad):
```python
# Create a span
span = tracer.start_span(name)  # ← obvious from code
```

## Error Handling

### Patterns

**Governance errors bubble up** — don't catch and suppress:
```python
# ✓ Let it propagate
try:
    verdict = await client.evaluate_event(event)
except OpenBoxNetworkError:
    if self.options.on_api_error == "fail_closed":
        raise GovernanceBlockedError("Network error in fail_closed mode") from None
    # fail_open: silently continue
```

**Always provide context in exceptions**:
```python
# ✓ Clear error message
raise GovernanceBlockedError(
    f"Policy blocked {activity_type} tool: {reason}"
)

# ✗ Vague
raise GovernanceBlockedError("Blocked")
```

**Use error types from openbox_langgraph** — don't define custom errors:
```python
from openbox_langgraph import (
    GovernanceBlockedError,
    GovernanceHaltError,
    GuardrailsValidationError,
    ApprovalRejectedError,
    ApprovalTimeoutError,
)
```

## Async/Await Patterns

### Async Functions

**Use `async def` everywhere possible** — prefer async over sync:
```python
# ✓ Preferred
async def handle_wrap_tool_call(
    middleware: OpenBoxMiddleware,
    tool_name: str,
    tool_input: dict,
) -> Any:
    verdict = await middleware.client.evaluate_event(event)
    ...
```

**Bridge sync → async when required** (LangChain hook constraints):
```python
def wrap_tool_call(self, tool_name: str, tool_input: dict) -> Any:
    """Sync hook that delegates to async via _run_async()."""
    return self._run_async(handle_wrap_tool_call(self, tool_name, tool_input))

def _run_async(self, coro: Coroutine) -> Any:
    """Run async in thread pool if inside event loop."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # Inside event loop → use thread pool
    return loop.run_in_executor(None, lambda: asyncio.run(coro))
```

### Context Propagation

**Manual trace context attachment across asyncio.Task boundaries**:
```python
# Bad: Context lost when LangGraph spawns new Task
span = tracer.start_span("tool_call")  # ← lost in new Task

# Good: Manually reattach context
async def _run_with_otel_context(
    context: trace.Context,
    task: Coroutine,
) -> Any:
    """Execute task with trace context attached."""
    with trace.use_span(context):
        return await task
```

## Testing Standards

### Test Framework

**Tool**: `pytest` with `pytest-asyncio`

**Async mode**:
```ini
[tool.pytest.ini_options]
asyncio_mode = "auto"
```

**Run tests**:
```bash
uv run pytest tests/ -v
uv run pytest tests/test_middleware.py::TestWrapToolCall -v
```

### Test Structure

**File**: `tests/test_middleware.py` (mirrors `openbox_deepagent/`)

**Class per hook**:
```python
class TestBeforeAgent:
    """Test before_agent / abefore_agent hook."""

    async def test_workflow_started_event_sent(self):
        """WorkflowStarted event fires before graph invocation."""
        ...

    async def test_pre_screen_guardrail_blocks_on_pii(self):
        """Guardrail pre-screen blocks PII in user message."""
        ...
```

### Mocking Strategy

**Mock governance client + tracing** — don't call real services:

```python
@pytest.fixture
def mock_governance_client():
    """Mock OpenBox governance client."""
    return AsyncMock(spec=GovernanceClient)

@pytest.fixture
async def middleware(mock_governance_client):
    """Middleware with mocked governance client."""
    mw = OpenBoxMiddleware(options, client=mock_governance_client)
    return mw
```

**Set mock return values explicitly**:
```python
mock_governance_client.evaluate_event.return_value = GovernanceVerdictResponse(
    verdict=Verdict.CONTINUE,
    reason=None,
)
```

## Code Organization

### Module Responsibilities

Keep each module focused:

| Module | Responsibility | Max LOC |
|---|---|---|
| `middleware.py` | Hook entry points + state management | 400 |
| `middleware_hooks.py` | Stateless hook implementations | 800 |
| `middleware_factory.py` | Configuration validation | 100 |
| `subagent_resolver.py` | Subagent detection | 150 |

### Private vs Public

**Public** (exported in `__init__.py`):
- `OpenBoxMiddleware`
- `OpenBoxMiddlewareOptions`
- `create_openbox_middleware()`
- `DEEPAGENT_BUILTIN_TOOLS`
- `DEEPAGENT_SUBAGENT_TOOL`

**Private** (internal implementation):
- `handle_*()` functions (hook logic)
- `_run_async()` (sync/async bridge)
- `_extract_*()` helpers
- All module-level functions not re-exported

## Performance Considerations

### Pre-screen Optimization

Cache first LLM call's guardrail response to avoid duplicate governance round-trip:

```python
# In abefore_agent (called once per invoke)
self._pre_screen_response = await client.evaluate_event(llm_started_event)

# In awrap_model_call (called for each LLM call)
if is_first_llm_call:
    # Reuse pre-screen response
    verdict = self._pre_screen_response
else:
    # Evaluate normally
    verdict = await client.evaluate_event(llm_started_event)
```

### Avoid N+1 Governance Calls

**Problem**: `hook_trigger` events fire for every HTTP request inside a tool. Without guards, your Rego policy fires twice per tool call (once for tool, once for each HTTP request).

**Solution**: Include `not input.hook_trigger` in your Rego policy:

```rego
result := {"decision": "BLOCK", "reason": "..."} if {
    input.event_type == "ActivityStarted"
    input.activity_type == "search_web"
    not input.hook_trigger  # ← Prevent double-fire on HTTP requests
}
```

## Debugging & Logging

### Debug Mode

Enable via environment variable:
```bash
OPENBOX_DEBUG=1 python agent.py
```

**Two log streams**:
1. `[OBX_EVENT]` (stderr) — Raw LangGraph events
2. `[OpenBox Debug]` (stdout) — Governance requests/responses

**When debugging**:
- Check if `workflow_type` matches your dashboard agent name
- Check if `subagent_name` is correct (not defaulted to "general-purpose")
- Verify `hook_trigger` guards in your Rego policy
- Look for duplicate event pairs (indicates double-fire)

## Dependency Management

### Adding Dependencies

Use `uv add` (not `pip install`):
```bash
uv add new-package
uv add --extra dev test-package
```

**This updates**:
- `pyproject.toml`
- `uv.lock` (commit this)

### Version Constraints

**Minimize upper bounds** (let transitive deps update):
```toml
dependencies = [
    "langchain-core>=0.3.0",  # ✓ No upper bound
    "langgraph>=0.2.0",
]
```

**Exception**: Direct dependency on unstable SDK:
```toml
dependencies = [
    "openbox-langgraph-sdk>=0.1.0",  # ← Sourced locally, version constraint optional
]
```

## Pre-commit Checklist

Before pushing:

1. **Code quality**:
   ```bash
   uv run ruff check --fix openbox_deepagent/ tests/
   uv run ruff format openbox_deepagent/ tests/
   uv run mypy openbox_deepagent/
   ```

2. **Tests pass**:
   ```bash
   uv run pytest tests/ -v
   ```

3. **No debug statements**:
   ```bash
   grep -r "print(" openbox_deepagent/ tests/  # Should return nothing
   grep -r "breakpoint()" openbox_deepagent/ tests/  # Should return nothing
   ```

4. **No credentials**:
   ```bash
   git diff HEAD | grep -i "api_key\|password\|secret"  # Should return nothing
   ```

5. **Commit message** (conventional format):
   ```
   feat: add HITL polling with configurable timeout
   fix: prevent double governance fire on http spans
   refactor: extract hook logic to middleware_hooks.py
   docs: update system architecture diagram
   test: add coverage for subagent conflict detection
   ```

## Documentation Standards

### README.md (User-Facing)

- Comprehensive, example-heavy (721 LOC)
- Keep as-is; it's already excellent
- Update when adding new public API

### CLAUDE.md (AI Assistant Instructions)

- Architecture overview
- Key design decisions
- Dependency notes
- Testing approach
- Update when refactoring core logic

### ./docs/

- **project-overview-pdr.md** — Product requirements + roadmap
- **codebase-summary.md** — File structure + LOC + public API
- **code-standards.md** — This file (style + testing conventions)
- **system-architecture.md** — Governance event flow + design patterns

---

**Last Updated**: 2026-03-21
**Version**: 0.1.0
