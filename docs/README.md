# Documentation Index — openbox-deepagent

This directory contains developer documentation for the openbox-deepagent SDK. Start here to navigate the knowledge base.

## Quick Navigation

### For Project Context
- **[project-overview-pdr.md](./project-overview-pdr.md)** — What this SDK is, why it exists, and where it's going
  - Project statement, value proposition, target audience
  - Product requirements (functional + non-functional)
  - Roadmap and open questions

### For New Developers
1. **[codebase-summary.md](./codebase-summary.md)** — File structure and module responsibilities
   - 2,288 LOC across 5 modules
   - Public API surface
   - Test coverage approach

2. **[code-standards.md](./code-standards.md)** — How to write code for this project
   - Style rules (Ruff, mypy strict, 100-char lines)
   - Naming conventions
   - Testing standards
   - Pre-commit checklist

### For Technical Deep-Dives
- **[system-architecture.md](./system-architecture.md)** — How the governance system works
  - Middleware lifecycle and event flow
  - Subagent detection and classification
  - Sync/async bridging and OTel span management
  - HITL polling and error handling

### For Planning & Future Development
- **[project-roadmap.md](./project-roadmap.md)** — Feature timeline and constraints
  - Current status (v0.1.0, Alpha)
  - Known limitations with workarounds
  - Planned features for v0.2.0 and v1.0.0
  - Open questions and decisions needed

## Reading Paths

### "I'm a new developer starting tomorrow"
1. Read [project-overview-pdr.md](./project-overview-pdr.md) (5 min)
2. Read [codebase-summary.md](./codebase-summary.md) (10 min)
3. Read [code-standards.md](./code-standards.md) (15 min)
4. Clone the repo and skim the source files (20 min)

**Total**: ~50 minutes to understand the codebase

### "I need to implement a feature"
1. Check [project-roadmap.md](./project-roadmap.md) to understand the scope
2. Review [system-architecture.md](./system-architecture.md) for relevant design patterns
3. Check [code-standards.md](./code-standards.md) for the pre-commit checklist

### "I'm debugging a governance issue"
1. Check [system-architecture.md](./system-architecture.md#debugging--observability) for diagnostics
2. Enable debug mode: `OPENBOX_DEBUG=1 python your_agent.py`
3. Check [codebase-summary.md](./codebase-summary.md#governance-event-flow) to trace the event flow

### "I'm making an architectural decision"
1. Read [system-architecture.md](./system-architecture.md#high-level-overview) for current design
2. Check [project-overview-pdr.md](./project-overview-pdr.md#implementation-guidance) for constraints
3. Consider impact on [project-roadmap.md](./project-roadmap.md) v0.2.0+ plans

## File Statistics

| File | LOC | Purpose |
|---|---|---|
| project-overview-pdr.md | 172 | Project requirements & product decisions |
| codebase-summary.md | 309 | Code structure & public API |
| code-standards.md | 504 | Development standards & style guide |
| system-architecture.md | 556 | Technical architecture & event flows |
| project-roadmap.md | 270 | Feature planning & timeline |
| **Total** | **1,811** | **Comprehensive knowledge base** |

## Related Documentation

- **[README.md](../README.md)** (721 LOC) — User-facing documentation for SDK usage
- **[CLAUDE.md](../CLAUDE.md)** (75 LOC) — AI assistant instructions for this project
- **[../plans/reports/](../plans/reports/)** — Research and analysis reports from project work

## Documentation Maintenance

These files are living documents updated alongside code changes:

- Update **project-overview-pdr.md** when requirements or version status changes
- Update **codebase-summary.md** when modules are added/removed
- Update **code-standards.md** when style rules or testing approach changes
- Update **system-architecture.md** when design patterns or event flows change
- Update **project-roadmap.md** with each release or when priorities shift

## Quick References

### Common Commands
```bash
# Install dependencies
uv sync --extra dev

# Run tests
uv run pytest tests/ -v

# Check code quality
uv run ruff check openbox_deepagent/ tests/
uv run mypy openbox_deepagent/

# Debug governance events
OPENBOX_DEBUG=1 python your_agent.py
```

### Project Structure
```
openbox_deepagent/
├── __init__.py              — Public API
├── middleware.py            — Core middleware class
├── middleware_factory.py    — Factory function
├── middleware_hooks.py      — Hook implementations
└── subagent_resolver.py     — Subagent detection

tests/
└── test_middleware.py       — Test suite (884 LOC)
```

### Public API
```python
from openbox_deepagent import (
    create_openbox_middleware,        # Factory function
    OpenBoxMiddleware,                # Middleware class
    OpenBoxMiddlewareOptions,         # Config TypedDict
    DEEPAGENT_BUILTIN_TOOLS,          # Constants
    # + 15 re-exports from openbox_langgraph
)
```

---

**Last Updated**: 2026-03-21 | **Version**: 0.1.0
