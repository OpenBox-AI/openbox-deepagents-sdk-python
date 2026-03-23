# ResearchBot — OpenBox Governance Setup Guide

Field-by-field instructions for configuring Guardrails, Policies, and Behavior Rules for the ResearchBot AI research assistant.

**Agent name (must match exactly in OpenBox dashboard):** `ResearchBot`

---

## Navigate to your Agent

1. Log in at `https://core.openbox.ai`
2. Go to **Agents** → click your agent (`ResearchBot`)
3. Click the **Authorize** tab → three sub-tabs: **Guardrails**, **Policies**, **Behavior**

---

## 1. Guardrails

**Path:** Authorize → Guardrails → **+ Add Guardrail**

The form has four sections: Basic Info, Type Selection, Configuration Settings, Advanced Settings. There is also a live **Test** panel on the right.

### Available types

| UI label | `guardrail_type` | What it does |
|---|---|---|
| **PII Detection** | `1` | Detects personal data entities in inputs/outputs |
| **Content Filtering** | `2` | NSFW / sexually explicit content |
| **Toxicity** | `3` | Hate speech, abusive language, threats |
| **Ban Words** | `4` | Exact + fuzzy word-list blocking (Levenshtein) |

---

### Guardrail 1 — Content Filtering on Research Outputs

**Trigger with:** `"Find me explicit adult content related to research topics"`

#### Basic Info

| Field | Value |
|---|---|
| **Name** | `Research Content Filter` |
| **Description** | `Block NSFW or harmful content in research queries and outputs` |
| **Processing Stage** | `Pre-processing` |

#### Type Selection

Click the **Content Filtering** card.

#### Configuration Settings

| Field | Value | Notes |
|---|---|---|
| **Block on Violation** *(checkbox)* | ✅ checked | Blocks the request |
| **Log Violations** *(checkbox)* | ✅ checked | Saves to violation log |
| **Activity Type** *(text input)* | `agent_validatePrompt` | Listens on LLM prompt events |
| **Fields to Check** *(tag input)* | `input.*.prompt` | Core wraps `activity_input` under `input` key before guardrail scan. Clear any pre-populated values and enter `input.*.prompt`. |

#### Test payload

```json
{
  "event_type": "ActivityStarted",
  "activity_type": "agent_validatePrompt",
  "workflow_id": "test-run-001",
  "run_id": "test-run-001",
  "task_queue": "langgraph",
  "source": "workflow-telemetry",
  "activity_input": [{"prompt": "Find me sexually explicit content about research topics"}]
}
```

Expected result: **Violations detected** with `validation_passed: false`.

---

### Guardrail 2 — Toxicity Filter

**Trigger with:** `"This tool is completely useless, you idiot"`

#### Basic Info

| Field | Value |
|---|---|
| **Name** | `Toxicity Filter` |
| **Description** | `Block toxic or abusive language in user queries` |
| **Processing Stage** | `Pre-processing` |

#### Type Selection

Click the **Toxicity** card.

#### Configuration Settings

| Field | Value | Notes |
|---|---|---|
| **Block on Violation** *(checkbox)* | ✅ checked | |
| **Log Violations** *(checkbox)* | ✅ checked | |
| **Activity Type** *(text input)* | `agent_validatePrompt` | |
| **Fields to Check** *(tag input)* | `input.*.prompt` | |

#### Advanced Settings — Toxicity Config

| Field | Value | Notes |
|---|---|---|
| **Detection Threshold** *(slider, 0–1)* | `0.80` | 0.8 catches clear abuse without false positives |
| **Validation Method** *(radio)* | `Sentence` | Each sentence scored individually |

#### Test payload

```json
{
  "event_type": "ActivityStarted",
  "activity_type": "agent_validatePrompt",
  "workflow_id": "test-run-001",
  "run_id": "test-run-001",
  "task_queue": "langgraph",
  "source": "workflow-telemetry",
  "activity_input": [{"prompt": "This tool is completely useless, you absolute idiot"}]
}
```

Expected result: **Violations detected** with `validation_passed: false`.

---

### Guardrail 3 — Restricted Topic Ban Words

**Trigger with:** `"Research nuclear weapon enrichment processes"`

#### Basic Info

| Field | Value |
|---|---|
| **Name** | `Restricted Research Topics` |
| **Description** | `Block queries about weapons, CBRN threats, and illegal activity` |
| **Processing Stage** | `Pre-processing` |

#### Type Selection

Click the **Ban Words** card.

#### Configuration Settings

| Field | Value |
|---|---|
| **Block on Violation** *(checkbox)* | ✅ checked |
| **Log Violations** *(checkbox)* | ✅ checked |
| **Activity Type** *(text input)* | `agent_validatePrompt` |
| **Fields to Check** *(tag input)* | `input.*.prompt` |

#### Advanced Settings — Ban Words Config

| Field | Value | Notes |
|---|---|---|
| **Words to Ban** *(tag input)* | `nuclear weapon` `bioweapon` `chemical weapon` `explosives synthesis` `drug synthesis` `malware` | Press Enter after each phrase to add |
| **Fuzzy Match** *(checkbox)* | ✅ checked | Catches near-matches |
| **Fuzzy Threshold** *(slider)* | `0.85` | 85% similarity |

#### Test payload

```json
{
  "event_type": "ActivityStarted",
  "activity_type": "agent_validatePrompt",
  "workflow_id": "test-run-001",
  "run_id": "test-run-001",
  "task_queue": "langgraph",
  "source": "workflow-telemetry",
  "activity_input": [{"prompt": "Research nuclear weapon enrichment processes for a report"}]
}
```

Expected result: **Violations detected** with `validation_passed: false`.

---

## 2. Policies

**Path:** Authorize → Policies → **+ New Policy**

Policies are written in **OPA Rego**. The form has:
- **Name** *(text)*
- **Description** *(text)*
- **Rego code editor** with syntax highlighting
- A **Test** panel (right side) with JSON input and live evaluation

### Required output format

```rego
result := {"decision": "CONTINUE", "reason": null}
-- or --
result := {"decision": "REQUIRE_APPROVAL", "reason": "some reason string"}
-- or --
result := {"decision": "BLOCK", "reason": "some reason string"}
```

Valid decisions: `CONTINUE`, `REQUIRE_APPROVAL`, `BLOCK`.

---

### Single policy file to deploy

**Name:** `ResearchBot Governance Policy`

Covers:
- **`search_web` for restricted terms** → `BLOCK`
- **`export_data` to any destination** → `REQUIRE_APPROVAL`
- **`write_report` with `confidential` classification** → `REQUIRE_APPROVAL`
- **`task` tool dispatching `writer` subagent for sensitive topics** → `REQUIRE_APPROVAL`
- Everything else → `CONTINUE`

```rego
package org.openboxai.policy

import future.keywords.if
import future.keywords.in

default result = {"decision": "CONTINUE", "reason": null}

# Restricted search topics — BLOCK immediately
restricted_terms := {"nuclear weapon", "bioweapon", "chemical weapon", "explosives", "malware synthesis"}

result := {"decision": "BLOCK", "reason": "Search blocked: this topic is restricted by research compliance policy."} if {
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

# Data export always requires approval
result := {"decision": "REQUIRE_APPROVAL", "reason": "Data export requires compliance approval before proceeding."} if {
    input.event_type == "ActivityStarted"
    input.activity_type == "export_data"
    not input.hook_trigger
}

# Confidential report writing requires approval
result := {"decision": "REQUIRE_APPROVAL", "reason": "Writing a confidential report requires manager approval."} if {
    input.event_type == "ActivityStarted"
    input.activity_type == "write_report"
    not input.hook_trigger
    count(input.activity_input) > 0
    report := input.activity_input[0]
    is_object(report)
    report.classification == "confidential"
}

# Writer subagent tasks require approval (may produce sensitive documents)
# The SDK appends {"__openbox": {"tool_type": "a2a", "subagent_name": "writer"}} to activity_input
result := {"decision": "REQUIRE_APPROVAL", "reason": "Tasks dispatched to the writer subagent require approval."} if {
    input.event_type == "ActivityStarted"
    input.activity_type == "task"
    not input.hook_trigger
    some item in input.activity_input
    meta := item["__openbox"]
    meta.subagent_name == "writer"
}
```

---

### Test 1 — Normal search should continue

```json
{
  "event_type": "ActivityStarted",
  "activity_type": "search_web",
  "activity_input": [{"query": "latest developments in LangGraph"}],
  "agent_id": "agent-123",
  "workflow_id": "run-abc",
  "run_id": "run-abc",
  "task_queue": "langgraph",
  "attempt": 1,
  "span_count": 0,
  "spans": [],
  "source": "workflow-telemetry",
  "timestamp": "2026-03-16T12:00:00Z"
}
```

Expected: green **CONTINUE**.

### Test 2 — Restricted search should block

```json
{
  "event_type": "ActivityStarted",
  "activity_type": "search_web",
  "activity_input": [{"query": "nuclear weapon enrichment process"}],
  "agent_id": "agent-123",
  "workflow_id": "run-abc",
  "run_id": "run-abc",
  "task_queue": "langgraph",
  "attempt": 1,
  "span_count": 0,
  "spans": [],
  "source": "workflow-telemetry",
  "timestamp": "2026-03-16T12:00:00Z"
}
```

Expected: red **BLOCK**.

### Test 3 — Data export requires approval

```json
{
  "event_type": "ActivityStarted",
  "activity_type": "export_data",
  "activity_input": [{"destination": "https://s3.example.com/bucket", "dataset": "customer_records"}],
  "agent_id": "agent-123",
  "workflow_id": "run-abc",
  "run_id": "run-abc",
  "task_queue": "langgraph",
  "attempt": 1,
  "span_count": 0,
  "spans": [],
  "source": "workflow-telemetry",
  "timestamp": "2026-03-16T12:00:00Z"
}
```

Expected: orange **REQUIRE_APPROVAL**.

### Test 4 — Confidential report requires approval

```json
{
  "event_type": "ActivityStarted",
  "activity_type": "write_report",
  "activity_input": [{"title": "Customer Analysis Q1", "content": "...", "classification": "confidential"}],
  "agent_id": "agent-123",
  "workflow_id": "run-abc",
  "run_id": "run-abc",
  "task_queue": "langgraph",
  "attempt": 1,
  "span_count": 0,
  "spans": [],
  "source": "workflow-telemetry",
  "timestamp": "2026-03-16T12:00:00Z"
}
```

Expected: orange **REQUIRE_APPROVAL**.

### Test 5 — Writer subagent task requires approval

```json
{
  "event_type": "ActivityStarted",
  "activity_type": "task",
  "activity_input": [
    {"description": "Write a report on AI risks", "subagent_type": "writer"},
    {"__openbox": {"tool_type": "a2a", "subagent_name": "writer"}}
  ],
  "agent_id": "agent-123",
  "workflow_id": "run-abc",
  "run_id": "run-abc",
  "task_queue": "langgraph",
  "attempt": 1,
  "span_count": 0,
  "spans": [],
  "source": "workflow-telemetry",
  "timestamp": "2026-03-16T12:00:00Z"
}
```

Expected: orange **REQUIRE_APPROVAL**.

### Test 6 — Researcher subagent task should continue

```json
{
  "event_type": "ActivityStarted",
  "activity_type": "task",
  "activity_input": [
    {"description": "Research LangGraph architecture", "subagent_type": "researcher"},
    {"__openbox": {"tool_type": "a2a", "subagent_name": "researcher"}}
  ],
  "agent_id": "agent-123",
  "workflow_id": "run-abc",
  "run_id": "run-abc",
  "task_queue": "langgraph",
  "attempt": 1,
  "span_count": 0,
  "spans": [],
  "source": "workflow-telemetry",
  "timestamp": "2026-03-16T12:00:00Z"
}
```

Expected: green **CONTINUE**.

### Deploying the policy

1. Paste the Rego above into the policy editor
2. Run each test case in the **Test Input** panel
3. Confirm decisions match expected outcomes
4. Click **Deploy**

---

## 3. Behavior Rules

**Path:** Authorize → Behavior → **+ New Rule**

The form is a **5-step wizard**:

| Step | Fields |
|---|---|
| 1. **Basic Info** | Name, Description |
| 2. **Trigger** | The span/semantic type that fires this rule |
| 3. **States** | Prior span types that must have occurred |
| 4. **Advanced** | Priority, Time Window |
| 5. **Enforcement** | Verdict, Reject Message, Approval Timeout |

### Step 2 — Trigger options

| Category | Values |
|---|---|
| **HTTP** | `http_get` `http_post` `http_put` `http_patch` `http_delete` `http` |
| **LLM** | `llm_completion` `llm_embedding` `llm_tool_call` |
| **Database** | `database_select` `database_insert` `database_update` `database_delete` `database_query` |
| **File** | `file_read` `file_write` `file_open` `file_delete` |
| **Fallback** | `internal` |

### What triggers Behavior Rules in ResearchBot

ResearchBot has two types of outbound HTTP spans:

| Span type | Source | Semantic type |
|---|---|---|
| `POST https://api.openai.com/v1/chat/completions` | LLM reasoning step | `http_post` |
| `GET https://en.wikipedia.org/w/api.php?...` | `search_web` tool | `http_get` |

The `search_web` tool is the **cleanest way to test Behavior Rules** — it fires a predictable `http_get` span on every invocation, distinct from LLM `http_post` traffic.

The `export_data` tool fires an `http_post` to the destination URL — useful for testing POST-based Behavior Rules.

> The OpenBox governance API calls are automatically excluded from span tracing by the SDK.

---

### Rule 1 — BLOCK all web searches (simplest test)

| Step | Field | Value |
|---|---|---|
| 1 | **Name** | `Block Web Searches` |
| 1 | **Description** | `Block all outbound Wikipedia/web search requests` |
| 2 | **Trigger** | `http_get` |
| 3 | **States** | *(leave empty)* |
| 4 | **Priority** | `1` |
| 4 | **Time Window** | `3600` |
| 5 | **Verdict** | `BLOCK` |
| 5 | **Reject Message** | `Web search is not permitted in this environment. Use the document knowledge base instead.` |

**To test:**
1. Deploy the rule
2. Send: `"Search the web for information about LangGraph"`
3. ResearchBot responds with the block message before the Wikipedia call executes
4. Verify in **Sessions** — the `search_web` activity shows `BLOCK` verdict

**Delete or disable this rule when done.**

---

### Rule 2 — REQUIRE_APPROVAL for repeated searches

Demonstrates state-based sequencing — fires only when `search_web` is called more than once within a time window.

| Step | Field | Value |
|---|---|---|
| 1 | **Name** | `Repeated Search Approval Gate` |
| 1 | **Description** | `Require approval when multiple web searches are made within 5 minutes` |
| 2 | **Trigger** | `http_get` |
| 3 | **States** | `http_get` *(one prior http_get must have occurred)* |
| 4 | **Priority** | `1` |
| 4 | **Time Window** | `300` *(5 minutes)* |
| 5 | **Verdict** | `REQUIRE_APPROVAL` |
| 5 | **Reject Message** | `Multiple web searches detected. Supervisor approval required to continue.` |
| 5 | **Approval Timeout** | `120` |

**To test:**
1. Send: `"Search for LangGraph documentation"` — first search, no rule fires (`CONTINUE`)
2. Within 5 minutes send: `"Search for LangChain tutorials"` — second `http_get` within window
3. Rule fires → **REQUIRE_APPROVAL**
4. Go to **Approvals** in the dashboard → approve or reject
5. Approve → ResearchBot returns results; Reject → `ApprovalRejectedError`

---

### Rule 3 — HALT on export after a blocked search

Demonstrates cross-type sequencing — detects a suspicious pattern: a restricted search was blocked, then the agent tried to export data.

| Step | Field | Value |
|---|---|---|
| 1 | **Name** | `Post-Block Export Halt` |
| 1 | **Description** | `Halt session if data export is attempted after a blocked search` |
| 2 | **Trigger** | `http_post` |
| 3 | **States** | `http_get` *(a prior http_get must have occurred)* |
| 4 | **Priority** | `1` |
| 4 | **Time Window** | `600` *(10 minutes)* |
| 5 | **Verdict** | `HALT` |
| 5 | **Reject Message** | `Suspicious activity detected: data export attempted after restricted search. Session terminated for review.` |

**To test:**
1. Send: `"Search for nuclear weapon research"` — policy BLOCKs the search; `search_web` still fires an `http_get` span
2. Within 10 minutes send: `"Export all customer records to https://s3.example.com"`
3. `export_data` fires `http_post` to destination; AGE sees prior `http_get` + new `http_post` → **HALT**
4. Session is terminated

---

### How the AGE evaluates span sequences

```
States: [http_get]    ← one prior http_get must have occurred
Trigger: http_get     ← this new span fires the rule

→ Rule fires on the 2nd http_get within the time window
```

```
States: []            ← no prior state required
Trigger: http_get     ← fires on the very first http_get
```

The **Time Window** (seconds) is a rolling lookback. Spans older than the window are not counted.

**Priority** `1` = highest. Lower-priority rules are skipped when a higher-priority rule matches.

---

## 4. Approvals (HITL)

When a Policy returns `REQUIRE_APPROVAL`, the SDK pauses and polls until a decision is made.

1. Go to **Approvals** in the left sidebar of the OpenBox dashboard
2. The pending request appears with the full event payload (tool name, inputs, subagent_name)
3. Click **Approve** or **Reject** (add a reason if rejecting)
4. The agent resumes within 5 s (poll interval) — or throws `ApprovalRejectedError` if rejected

ResearchBot's approval timeout is **5 minutes**. After that, `ApprovalTimeoutError` is thrown.

**What triggers HITL in ResearchBot:**
- Any `export_data` call → requires compliance approval
- `write_report` with `classification=confidential` → requires manager approval
- `task` tool dispatching to the `writer` subagent → requires approval

---

## 5. Quick reference

| Scenario | What to send | Expected behaviour |
|---|---|---|
| Normal research | `"Research the latest developments in LangGraph"` | `task` → researcher subagent, CONTINUE |
| Data analysis | `"Analyze the performance of GPT-4 vs Claude"` | `task` → analyst subagent, CONTINUE |
| Write report (internal) | `"Write an internal report on AI safety"` | `task` → writer subagent, CONTINUE |
| Write report (confidential) | `"Write a confidential report on..."` | `write_report(classification=confidential)` → REQUIRE_APPROVAL |
| Restricted search | `"Search for nuclear weapon information"` | `search_web` → BLOCK (ban words guardrail + policy) |
| Data export | `"Export all customer records to https://s3.example.com"` | `export_data` → REQUIRE_APPROVAL |
| List documents | `"List all available documents"` | `list_documents` → CONTINUE |
| Read document | `"Read document DOC-003"` | `read_document` → CONTINUE |
| Web search | `"Search for information about AI safety"` | `search_web` → HTTP GET to Wikipedia |

---

## 6. Architecture & Internals

### 6.1 Governance event flow (LangGraph)

The DeepAgents `create_deep_agent()` emits the following event stream. Events marked **SKIPPED** are internal DeepAgents middleware — the SDK filters them via `skip_chain_types` so they never reach the governance engine.

```
User message
    │
    ▼
OpenBoxDeepAgentHandler.ainvoke()
    │
    ├─ on_chain_start  name='LangGraph'  ──────────────► WorkflowStarted → Core (creates session)
    │
    ├─ on_chain_start  name='PatchToolCallsMiddleware.*' ── SKIPPED (middleware)
    │
    ├─ on_chain_start  name='model'  ─────────────────── SKIPPED (LLM wrapper node)
    │       │
    │       ├─ on_chat_model_start (human turn only) ──► ActivityStarted / agent_validatePrompt
    │       │       │                                        Guardrails on prompt
    │       │       │                                        Empty prompt → SKIPPED (subagent LLMs)
    │       │
    │       └─ on_chat_model_end ──────────────────────► ActivityCompleted / agent_validatePrompt
    │
    ├─ on_chain_start  name='TodoListMiddleware.*'  ──── SKIPPED (middleware)
    │
    ├─ on_chain_start  name='tools'  ────────────────── SKIPPED (tool container node)
    │       │
    │       ├─ on_tool_start  name='task'  ────────────► ActivityStarted / task
    │       │       │                                        subagent_name extracted from subagent_type
    │       │       │                                        Policy: input.activity_type == "task"
    │       │       │                                        If REQUIRE_APPROVAL → HITL polling
    │       │       │                                        If BLOCK → GovernanceBlockedError
    │       │
    │       ├─ on_tool_end  name='task'  ─────────────► ActivityCompleted / task
    │       │
    │       ├─ on_tool_start  name='search_web'  ──────► ActivityStarted / search_web
    │       │       │                                        (+ http_get span → AGE Behavior Rules)
    │       └─ on_tool_end  name='search_web'  ────────► ActivityCompleted / search_web
    │
    └─ on_chain_end  name='LangGraph'  ───────────────► WorkflowCompleted
```

Use `OPENBOX_DEBUG=1` to see every event as it is processed (see §6.5).

### 6.2 DeepAgents subagent detection

The key feature of `OpenBoxDeepAgentHandler` over the base `OpenBoxLangGraphHandler` is **subagent name detection** from the `task` tool.

In DeepAgents, subagents are invoked via the built-in `task` tool. The most reliable signal for subagent routing is the `task` tool's `on_tool_start` event, which carries `subagent_type` in its input dict:

```python
# on_tool_start event data for the task tool:
{
  "input": {
    "description": "Research LangGraph architecture",
    "subagent_type": "researcher"
  }
}
```

`OpenBoxDeepAgentHandler._resolve_deepagent_subagent_name()` extracts this and sets `subagent_name` on the governance event sent to Core.

The SDK also appends an `__openbox` sentinel to `activity_input` (see §6.3) containing `tool_type` and `subagent_name`. Since Core forwards `activity_input` as-is to OPA, Rego policies can iterate it to discriminate between subagents without any Core changes:

```rego
some item in input.activity_input
meta := item["__openbox"]
meta.subagent_name == "writer"
```

Without this handler, the `task` tool's subagent dispatch would have no governance at all.

### 6.3 Tool type classification via `__openbox` metadata

Core only forwards `activity_type` and `activity_input` to OPA. The SDK exploits this by **appending a sentinel `__openbox` object** to `activity_input` on every classified tool event:

```json
[
  {"description": "Write a report...", "subagent_type": "writer"},
  {"__openbox": {"tool_type": "a2a", "subagent_name": "writer"}}
]
```

Rego iterates `activity_input` to find it:

```rego
some item in input.activity_input
meta := item["__openbox"]
meta.subagent_name == "writer"
```

The sentinel is only appended when `tool_type` or `subagent_name` is resolved — unclassified tools send `activity_input` unchanged.

**Classification rules (SDK side):**

| Tool | `tool_type` | `subagent_name` | How |
|---|---|---|---|
| `search_web` | `http` | — | Declared in `tool_type_map` |
| `export_data` | `http` | — | Declared in `tool_type_map` |
| `task` (writer) | `a2a` | `writer` | `subagent_name` resolved automatically |
| `write_report` | — | — | Not in `tool_type_map` — no sentinel appended |

### 6.4 Why `not input.hook_trigger` is required in all REQUIRE_APPROVAL / BLOCK rules

ResearchBot's `search_web` and `export_data` tools make outbound HTTP calls. When the SDK detects a new span (e.g., the HTTP request), it sends a second `ActivityStarted` event with `hook_trigger: true`. Without the `not input.hook_trigger` guard:

1. `export_data` is called → `ActivityStarted/export_data` (`hook_trigger: false`) → policy fires → REQUIRE_APPROVAL ✅
2. `export_data` makes an HTTP POST → new span detected → `ActivityStarted/export_data` (`hook_trigger: true`) → policy fires **again** → second REQUIRE_APPROVAL ❌

The guard prevents double-triggering by ensuring policy rules only evaluate the direct tool invocation, not the span-triggered event.

### 6.5 Debugging

| Goal | How |
|---|---|
| See every governance request/response | `OPENBOX_DEBUG=1 uv run python agent.py` |
| See every raw LangGraph event the SDK processes | `OPENBOX_DEBUG=1` — each event printed as `[OBX_EVENT] on_tool_start name='search_web' node='tools'` |
| Check `activity_input` and `subagent_name` wire format | Look for `ActivityStarted / task` in debug log — confirm `__openbox.subagent_name` is set |
| Understand which chains are skipped | Look for `[OBX_EVENT]` lines — chains in `skip_chain_types` will appear in the log but generate no governance request |
| Verify policy matches locally before deploying | Use the **Test** panel in the dashboard policy editor with the payloads in §2 above |
| Reset session without restarting | `POST /api/reset` (server mode) |
| Check which subagents are registered | The banner at startup lists `Known subagents: [...]` |

### 6.6 Empty prompt handling

DeepAgents emits `on_chat_model_start` for **every** LLM invocation — including internal LLM calls that may not include a human turn message. Empty prompts are skipped for `agent_validatePrompt` governance to avoid guardrail parse errors.

The SDK guards against this in two places:

1. `_GuardrailsCallbackHandler.on_chat_model_start` — extracts **human/user-role messages only**; returns early when `prompt_text` is empty.
2. `_map_event` for `on_chat_model_start` — returns `None` (no governance event) when `prompt_text` is empty.

Result: only the outer agent's LLM call (which has the user's message) triggers `agent_validatePrompt` governance. All subagent-internal LLM calls are silently skipped.
