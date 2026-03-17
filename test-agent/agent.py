"""
OpenBox DeepAgents SDK — ResearchBot: AI Research Assistant

A multi-turn research assistant that showcases OpenBox governance
for DeepAgents-style LangGraph agents:

  GUARDRAILS  — Content filtering on research outputs
  POLICIES    — BLOCK on restricted research topics (e.g. weapons)
  HITL        — Sensitive exports require human approval
  SUBAGENTS   — `task` tool routes to named subagents; governance sees subagent_type

The agent simulates the DeepAgents `task` tool pattern using a LangGraph
ReAct agent (create_react_agent). The `task` tool dispatches to named
subagents — governance events include `subagent_name` so Rego policies
can target specific subagent types.

Subagents:
  - researcher       -- web search and summarization
  - analyst          -- data analysis and comparison
  - writer           -- drafting reports and documents
  - general-purpose  -- catch-all (DeepAgents default)

Tools (non-task):
  search_web         -- Real HTTP GET to Wikipedia (triggers Behavior Rules)
  read_document      -- Read from mock knowledge base
  write_report       -- Write a research report
  list_documents     -- List available documents
  export_data        -- Export data (REQUIRE_APPROVAL gate)

Try these prompts:
  "Research the latest developments in LangGraph"
  "Analyze the performance of GPT-4 vs Claude"
  "Write a report on AI safety research"           <- writer subagent (HITL if configured)
  "Search for information about nuclear weapons"   <- BLOCK (restricted topic)
  "Export all customer records to external S3"     <- REQUIRE_APPROVAL
  "List all available documents"
  "What is the status of all tasks?"
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import http.server
import threading
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

from openbox_deepagent import (
    DEEPAGENT_BUILTIN_TOOLS,
    DEEPAGENT_SUBAGENT_TOOL,
    ApprovalRejectedError,
    ApprovalTimeoutError,
    GovernanceBlockedError,
    GovernanceHaltError,
    GuardrailsValidationError,
    OpenBoxDeepAgentHandler,
    create_openbox_deep_agent_handler,
)

load_dotenv()

# ─── Mock data store ──────────────────────────────────────────────

DOCUMENTS: dict[str, dict[str, Any]] = {
    "DOC-001": {
        "id": "DOC-001",
        "title": "Introduction to LangGraph",
        "content": (
            "LangGraph is a library for building stateful, multi-actor applications with LLMs.\n"
            "It extends LangChain with graph-based orchestration, allowing developers to create\n"
            "complex workflows with cycles, branches, and human-in-the-loop patterns.\n\n"
            "Key features:\n"
            "- Persistent state across turns (checkpointing)\n"
            "- Parallel node execution\n"
            "- Human-in-the-loop interrupts\n"
            "- Streaming support for long-running tasks\n"
            "- Built-in support for multi-agent architectures"
        ),
        "author": "LangChain Team",
        "created_at": "2025-01-15",
        "tags": ["langgraph", "langchain", "llm", "agents"],
    },
    "DOC-002": {
        "id": "DOC-002",
        "title": "AI Safety Research Overview",
        "content": (
            "AI Safety research focuses on ensuring AI systems behave as intended and remain\n"
            "beneficial as they become more capable. Key areas include:\n\n"
            "1. Alignment: Ensuring AI goals match human values\n"
            "2. Interpretability: Understanding what AI systems have learned\n"
            "3. Robustness: Making AI systems reliable under distribution shift\n"
            "4. Governance: Policy frameworks for responsible AI development\n\n"
            "Major organizations: Anthropic, OpenAI Safety Team, DeepMind Safety, CHAI, MIRI"
        ),
        "author": "Safety Research Team",
        "created_at": "2025-02-20",
        "tags": ["ai-safety", "alignment", "research"],
    },
    "DOC-003": {
        "id": "DOC-003",
        "title": "OpenBox Governance Architecture",
        "content": (
            "OpenBox provides real-time governance for AI agent workflows. The platform\n"
            "evaluates agent actions against configurable policies before execution.\n\n"
            "Components:\n"
            "- Policy Engine: Rego-based rules for fine-grained control\n"
            "- Guardrails: PII detection, content filtering, toxicity screening\n"
            "- HITL: Human approval workflows for sensitive operations\n"
            "- Behavior Rules: Hook-level governance for HTTP calls and file I/O\n"
            "- Audit Log: Complete record of all governance decisions"
        ),
        "author": "OpenBox Team",
        "created_at": "2025-03-01",
        "tags": ["openbox", "governance", "ai-safety"],
    },
    "DOC-004": {
        "id": "DOC-004",
        "title": "Customer Database Export Procedures",
        "content": (
            "CONFIDENTIAL — INTERNAL USE ONLY\n\n"
            "This document describes procedures for exporting customer data.\n"
            "All exports require manager approval and compliance review.\n\n"
            "Data classification: PII Level 3 — Restricted\n"
            "Approval workflow: Manager → Compliance → Legal → DPO\n\n"
            "Export endpoints:\n"
            "  - Internal BI: analytics.internal.company.com\n"
            "  - Approved partners: See partner registry\n"
            "  - External storage: BLOCKED — requires exceptional approval"
        ),
        "author": "Compliance Team",
        "created_at": "2025-01-10",
        "tags": ["confidential", "compliance", "customer-data", "pii"],
    },
}

_task_store: dict[str, dict[str, Any]] = {}
_report_store: dict[str, str] = {}


# ─── Subagent simulation ──────────────────────────────────────────

async def _simulate_subagent(subagent_type: str, description: str, task_id: str) -> str:
    """Simulate a subagent HTTP call — real HTTP GET so Behavior Rules fire."""
    import httpx  # lazy

    url = f"https://api.research-mock.internal/subagent/{subagent_type}"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(url, json={"task": description, "task_id": task_id})
    except Exception:
        pass  # Mock endpoint; real governance hooks still fire on the attempt

    responses = {
        "researcher": (
            f'Research completed on: "{description}"\n\n'
            "Findings: Based on available sources, this topic has seen significant recent "
            "developments. Key papers identified. Primary sources validated. Confidence: HIGH."
        ),
        "analyst": (
            f'Analysis completed on: "{description}"\n\n'
            "Key metrics compared. Statistical significance assessed. "
            "Data supports option A over B by ~23% on primary KPIs."
        ),
        "writer": (
            f'Draft document created for: "{description}"\n\n'
            "Executive summary written. Intro and background complete. "
            "Methodology drafted. Conclusions and recommendations prepared. ~1,200 words."
        ),
        "general-purpose": (
            f'Task completed: "{description}"\n\n'
            "All requested actions performed. Results available in the output store."
        ),
    }
    return responses.get(
        subagent_type,
        f'Task completed by {subagent_type} subagent: "{description}"',
    )


# ─── Tools ───────────────────────────────────────────────────────

@tool
async def task(description: str, subagent_type: str) -> str:
    """Dispatch a task to a specialized subagent.

    Use this for complex research, analysis, or writing tasks that benefit
    from specialist expertise. Available subagent types:
      - researcher       : web research and summarization
      - analyst          : data analysis and comparison
      - writer           : drafting reports and structured documents
      - general-purpose  : catch-all for miscellaneous tasks

    Args:
        description: Clear description of the task to be performed.
        subagent_type: One of: researcher, analyst, writer, general-purpose.
    """
    task_id = f"TASK-{datetime.now(timezone.utc).strftime('%H%M%S%f')[:12]}"
    _task_store[task_id] = {
        "id": task_id,
        "subagent": subagent_type,
        "description": description,
        "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    print(f"  [task] dispatching → {subagent_type}: {description[:60]}...")

    try:
        result = await _simulate_subagent(subagent_type, description, task_id)
        _task_store[task_id].update(
            status="completed",
            result=result,
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
        print(f"  [task] {task_id} completed by {subagent_type}")
        return f"Task {task_id} completed.\n\n{result}"
    except Exception as exc:
        _task_store[task_id].update(
            status="failed",
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
        return f"Task {task_id} failed: {exc}"


@tool
async def search_web(query: str) -> str:
    """Search the web for information on a topic.

    Makes a real HTTP GET request to Wikipedia — triggers Behavior Rules
    governance if configured in OpenBox.

    Args:
        query: The search query string.
    """
    import httpx  # lazy

    print(f"  [search_web] query: {query}")
    url = (
        "https://en.wikipedia.org/w/api.php"
        f"?action=opensearch&search={query}&limit=3&format=json"
    )
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            res = await client.get(url, headers={"User-Agent": "OpenBox-ResearchBot/1.0"})
        if res.is_success:
            data = res.json()
            titles: list[str] = data[1]
            descriptions: list[str] = data[2]
            if not titles:
                return f'No Wikipedia results found for "{query}".'
            results = "\n".join(
                f"{i + 1}. {t}\n   {descriptions[i]}"
                for i, t in enumerate(titles)
            )
            return f'Search results for "{query}":\n\n{results}'
    except Exception:
        pass

    return (
        f'Search results for "{query}":\n\n'
        f"1. {query} — Overview\n   A comprehensive overview of the topic.\n"
        f"2. Recent developments in {query}\n   Latest research and news.\n"
        f"3. {query}: A deep dive\n   Technical analysis and expert perspectives."
    )


@tool
def read_document(document_id: str) -> str:
    """Read a document from the knowledge base by its ID.

    Args:
        document_id: A document ID such as "DOC-001". Use list_documents to
                     see all available IDs.
    """
    print(f"  [read_document] id: {document_id}")
    doc = DOCUMENTS.get(document_id.upper())
    if not doc:
        available = ", ".join(DOCUMENTS.keys())
        return f'Document "{document_id}" not found. Available: {available}'
    return "\n".join([
        f"Document : {doc['title']} ({doc['id']})",
        f"Author   : {doc['author']}",
        f"Created  : {doc['created_at']}",
        f"Tags     : {', '.join(doc['tags'])}",
        "",
        doc["content"],
    ])


@tool
def list_documents() -> str:
    """List all documents available in the knowledge base."""
    print(f"  [list_documents] listing {len(DOCUMENTS)} documents")
    lines = [
        f"  {d['id']}  {d['title']:<42}  [{', '.join(d['tags'][:2])}]"
        for d in DOCUMENTS.values()
    ]
    return f"Available documents ({len(lines)}):\n\n" + "\n".join(lines)


@tool
def write_report(title: str, content: str, classification: str = "internal") -> str:
    """Write and save a research report to the output store.

    Args:
        title: The report title.
        content: The full report content.
        classification: One of: public, internal, confidential.
    """
    report_id = f"RPT-{datetime.now(timezone.utc).strftime('%H%M%S%f')[:12]}"
    _report_store[report_id] = content
    print(f"  [write_report] created {report_id}: {title} [{classification}]")
    return "\n".join([
        "Report saved successfully.",
        f"  Report ID      : {report_id}",
        f"  Title          : {title}",
        f"  Classification : {classification}",
        f"  Length         : {len(content)} characters",
        f"  Created        : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
    ])


@tool
async def export_data(destination: str, dataset: str) -> str:
    """Export a dataset to an external destination.

    This operation is sensitive — OpenBox governance will gate it with
    REQUIRE_APPROVAL if configured. Writes to external storage may be BLOCKED.

    Args:
        destination: The export destination (URL or storage path).
        dataset: The name of the dataset to export.
    """
    print(f"  [export_data] {dataset} → {destination}")

    # Real HTTP POST — triggers Behavior Rules governance
    import httpx  # lazy

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(
                destination,
                json={"dataset": dataset, "requested_at": datetime.now(timezone.utc).isoformat()},
                headers={"Content-Type": "application/json"},
            )
    except Exception:
        pass  # Governance hooks still fire on the outbound request attempt

    export_id = f"EXP-{datetime.now(timezone.utc).strftime('%H%M%S%f')[:12]}"
    return "\n".join([
        "Export completed.",
        f"  Export ID   : {export_id}",
        f"  Dataset     : {dataset}",
        f"  Destination : {destination}",
        f"  Records     : 1,247",
        f"  Status      : COMPLETED",
    ])


@tool
def get_task_status(task_id: str = "all") -> str:
    """Check the status of a previously dispatched task.

    Args:
        task_id: A task ID like "TASK-123456", or "all" to see all tasks.
    """
    if task_id.lower() == "all":
        if not _task_store:
            return "No tasks dispatched yet."
        rows = [
            f"  {t['id']}  [{t['status'].upper():<9}]  {t['subagent']:<15}  {t['description'][:50]}"
            for t in _task_store.values()
        ]
        return f"Tasks ({len(rows)}):\n\n" + "\n".join(rows)

    record = _task_store.get(task_id.upper())
    if not record:
        return f'Task "{task_id}" not found. Use task_id="all" to list all tasks.'
    lines = [
        f"Task     : {record['id']}",
        f"Subagent : {record['subagent']}",
        f"Status   : {record['status'].upper()}",
        f"Started  : {record['started_at']}",
    ]
    if record.get("completed_at"):
        lines.append(f"Completed: {record['completed_at']}")
    if record.get("result"):
        lines.append(f"\nResult:\n{record['result']}")
    return "\n".join(lines)


# ─── Governance error handler ─────────────────────────────────────

def _handle_governance_error(err: Exception) -> dict[str, Any]:
    if isinstance(err, GovernanceHaltError):
        return {
            "output": f"This session has been terminated by our compliance system: {err}",
            "governance": {"status": "halt", "reason": str(err)},
        }
    if isinstance(err, GovernanceBlockedError):
        return {
            "output": f"I'm unable to process this request: {err}",
            "governance": {"status": "blocked", "reason": str(err)},
        }
    if isinstance(err, GuardrailsValidationError):
        reason = "; ".join(err.reasons)
        return {
            "output": reason,
            "governance": {"status": "guardrails", "reason": reason},
        }
    if isinstance(err, ApprovalTimeoutError):
        return {
            "output": "This request requires approval but the approval window has expired.",
            "governance": {"status": "hitl_timeout"},
        }
    if isinstance(err, ApprovalRejectedError):
        return {
            "output": f"This request was reviewed and declined. {err}".strip(),
            "governance": {"status": "hitl_rejected", "reason": str(err)},
        }
    return {
        "output": f"An error occurred: {err}",
        "governance": {"status": "error", "reason": str(err)},
    }


# ─── Core run function ────────────────────────────────────────────

async def run_turn(
    handler: OpenBoxDeepAgentHandler,
    user_input: str,
    thread_id: str,
) -> dict[str, Any]:
    try:
        result = await handler.ainvoke(
            {"messages": [{"role": "user", "content": user_input}]},
            config={"configurable": {"thread_id": thread_id}},
        )
        messages = result.get("messages", [])
        output = ""
        if messages:
            last = messages[-1]
            if hasattr(last, "content"):
                output = last.content
            elif isinstance(last, dict):
                output = last.get("content", "")
        return {"output": output or "(no response)", "governance": {"status": "allow"}}
    except Exception as err:
        return _handle_governance_error(err)


# ─── Main ─────────────────────────────────────────────────────────

async def main() -> None:
    openai_key = os.environ.get("OPENAI_API_KEY")
    openbox_url = os.environ.get("OPENBOX_URL")
    openbox_api_key = os.environ.get("OPENBOX_API_KEY")

    if not openai_key:
        print("Error: OPENAI_API_KEY is required.", file=sys.stderr)
        sys.exit(1)
    if not openbox_url:
        print("Error: OPENBOX_URL is required.", file=sys.stderr)
        sys.exit(1)
    if not openbox_api_key:
        print("Error: OPENBOX_API_KEY is required.", file=sys.stderr)
        sys.exit(1)

    # ── Banner ────────────────────────────────────────────────────
    print("╔══════════════════════════════════════════════════════════╗")
    print("║     ResearchBot — AI Research Assistant (OpenBox)        ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print(f"OpenBox : {openbox_url}")
    print(f"Key     : {openbox_api_key[:10]}...")
    print()
    print("Governance features active:")
    print("  • Subagents   — task tool routes to researcher/analyst/writer/general-purpose")
    print("  • Guardrails  — Content filtering on research outputs")
    print("  • Policies    — BLOCK on restricted topics (weapons, etc.)")
    print("  • HITL        — Sensitive exports require human approval")
    print("  • Behavior    — HTTP calls governed via Behavior Rules")
    print()
    print("Try:")
    print('  "Research the latest developments in LangGraph"')
    print('  "Analyze the performance of GPT-4 vs Claude"')
    print('  "Write a report on AI safety research"')
    print('  "Search for information about nuclear weapons"     <- BLOCK')
    print('  "Export all customer records to https://s3.example.com" <- REQUIRE_APPROVAL')
    print('  "List all available documents"')
    print('  "What is the status of all tasks?"')
    print('  Type "exit" or "quit" to end the session.')
    print()

    # ── LangGraph graph ───────────────────────────────────────────
    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0, api_key=openai_key)

    all_tools = [task, search_web, read_document, list_documents,
                 write_report, export_data, get_task_status]

    graph = create_react_agent(llm, all_tools)
    print("✓ LangGraph ReAct agent compiled")

    # ── OpenBox DeepAgent handler ─────────────────────────────────
    try:
        governed = await create_openbox_deep_agent_handler(
            graph=graph,
            api_url=openbox_url,
            api_key=openbox_api_key,
            agent_name="ResearchBot",
            validate=True,
            on_api_error="fail_open",
            known_subagents=["researcher", "analyst", "writer", "general-purpose"],
            guard_interrupt_on_conflict=True,
            skip_chain_types={"agent", "call_model", "RunnableSequence", "Prompt", "ChatPromptTemplate"},
            hitl={
                "enabled": True,
                "poll_interval_ms": 5_000,
                "max_wait_ms": 300_000,
            },
        )
        print("✓ OpenBox DeepAgent governance handler ready")
        print(f"  Known subagents: {governed.get_known_subagents()}")
    except Exception as err:
        print(f"✗ Failed to initialise OpenBox handler: {err}", file=sys.stderr)
        sys.exit(1)

    # ── Mode selection ────────────────────────────────────────────
    server_mode = (
        os.environ.get("SERVER_MODE") == "true"
        or "--server" in sys.argv
    )

    thread_id = f"researchbot-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"

    if server_mode:
        await _run_server(governed, thread_id)
    else:
        await _run_repl(governed, thread_id)


# ─── REPL mode ────────────────────────────────────────────────────

async def _run_repl(governed: OpenBoxDeepAgentHandler, thread_id: str) -> None:
    print("\n" + "═" * 62)
    print("Session started. How can ResearchBot help you today?")
    print("═" * 62 + "\n")

    session_halted = False

    while not session_halted:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit", "bye"):
            print("\nResearchBot: Goodbye!\n")
            break

        print("\nResearchBot: thinking...\n")
        turn = await run_turn(governed, user_input, thread_id)
        print(f"ResearchBot: {turn['output']}")

        gov = turn.get("governance", {})
        if gov.get("status") not in (None, "allow"):
            print(f"        [{gov['status'].upper()}] {gov.get('reason', '')}")
        if gov.get("status") == "halt":
            session_halted = True
            print("\nSession terminated by compliance policy.")

        print()

    print("─" * 62)
    print("Session ended.")


# ─── HTTP server mode ─────────────────────────────────────────────

async def _run_server(governed: OpenBoxDeepAgentHandler, thread_id: str) -> None:
    """Run an HTTP server in a background thread, dispatching async work back
    onto the main event loop via run_coroutine_threadsafe — avoids nested
    asyncio.run() errors that occur when calling async code from a sync
    HTTP handler that lives on a different thread.
    """
    import json as _json
    import concurrent.futures
    from http.server import BaseHTTPRequestHandler, HTTPServer

    loop = asyncio.get_running_loop()
    session_state = {"halted": False, "thread_id": thread_id}
    governed_ref = governed

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            pass  # suppress default access logs

        def _send_json(self, code: int, data: dict[str, Any]) -> None:
            body = _json.dumps(data).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self) -> None:  # noqa: N802
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/api/health":
                self._send_json(200, {"ok": True, "halted": session_state["halted"]})
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self) -> None:  # noqa: N802
            if self.path == "/api/reset":
                session_state["halted"] = False
                session_state["thread_id"] = f"researchbot-{datetime.now().strftime('%H%M%S')}"
                self._send_json(200, {"ok": True})
                return

            if self.path == "/api/chat":
                if session_state["halted"]:
                    self._send_json(200, {
                        "output": "Session terminated. Please reset to start a new session.",
                        "governance": {"status": "halt"},
                    })
                    return

                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                try:
                    data = _json.loads(body)
                    message: str = data["message"]
                except (ValueError, KeyError):
                    self._send_json(400, {"error": 'Bad request — expected {"message": "..."}'})
                    return

                print(f"\n[User] {message}")

                # Dispatch the async coroutine onto the main event loop from this
                # handler thread, then block here until it completes.
                future = asyncio.run_coroutine_threadsafe(
                    run_turn(governed_ref, message, session_state["thread_id"]),
                    loop,
                )
                turn = future.result()  # blocks the handler thread only

                if turn.get("governance", {}).get("status") == "halt":
                    session_state["halted"] = True

                output = turn["output"]
                print(f"[ResearchBot] {output[:120]}{'…' if len(output) > 120 else ''}")
                self._send_json(200, turn)
                return

            self.send_response(404)
            self.end_headers()

    port = int(os.environ.get("PORT", "3142"))
    server = HTTPServer(("", port), Handler)

    print("\n" + "═" * 62)
    print(f"ResearchBot HTTP server listening on http://localhost:{port}")
    print("Endpoints:")
    print('  POST /api/chat    { "message": "..." }')
    print("  POST /api/reset")
    print("  GET  /api/health")
    print("═" * 62)

    # Run the blocking server in a background thread; keep the event loop alive.
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    try:
        # Keep the event loop spinning so run_coroutine_threadsafe works.
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        server.shutdown()
        print("\nServer shut down.")


if __name__ == "__main__":
    asyncio.run(main())
