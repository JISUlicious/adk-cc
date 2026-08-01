"""Spawnable sub-agents: parallel fan-out with wait-all default + early resume.

Lineage: `feat/agent-tool-parallel-explore` (2026-06) proved the core — ADK
dispatches N AgentTool calls concurrently, each in an isolated session, and an
enriched envelope makes unordered parallel results attributable. Parked for
priority, reopened 2026-08-01 with a changed shape (user decisions):

  * ONE merged explorer (code + web), read-only tools only, and nothing
    interactive — no ask_user_question, no confirmations. A sub-agent has no
    human to talk to; the permission plugin converts any would-ask into a
    structured deny (see `plugins/permissions.py`).
  * The coordinator WAITS for the whole batch by default, but may resume early
    when a subset already answers the question. That rules out the one-call-
    per-explorer AgentTool shape — ADK's dispatcher gathers ALL tool calls
    before the model sees anything, so "enough already" has no decision point.
    Hence two tools:

        spawn_explorers(tasks=[...])       -> returns ids immediately
        collect_explorers(wait=..., ...)   -> "all" (default) or "first_done"

    The judgment of "enough" stays with the model, exercised BETWEEN collects
    rather than inside an opaque gather.
  * The UI must show that spawned agents are running — the spawn result and
    the pending collect give the thread everything it needs (see AgentCard).

Children run inside the turn. Whatever the model leaves uncollected is
cancelled at invocation end (plugin hook) and on session abort (broker) — a
stray explorer must not outlive the turn that spawned it, and delete-mid-run
(#87) must kill the whole tree.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from typing import Any, Optional

from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types

_log = logging.getLogger(__name__)


# --- concurrency cap (ported) ---------------------------------------------
_DEFAULT_MAX = 8


def concurrency_limit() -> int:
    try:
        return max(1, int(os.environ.get("ADK_CC_SUBAGENTS_MAX", _DEFAULT_MAX)))
    except (TypeError, ValueError):
        return _DEFAULT_MAX


_gate: Optional[asyncio.Semaphore] = None


def _sem() -> asyncio.Semaphore:
    global _gate
    if _gate is None:
        _gate = asyncio.Semaphore(concurrency_limit())
    return _gate


def _reset_gate_for_test(n: int) -> None:
    global _gate
    _gate = asyncio.Semaphore(n)


# --- registry --------------------------------------------------------------
class _Child:
    __slots__ = ("id", "task_text", "atask", "started", "invocation_id")

    def __init__(self, id: str, task_text: str, atask: asyncio.Task,
                 invocation_id: str) -> None:
        self.id, self.task_text, self.atask = id, task_text, atask
        self.started = time.perf_counter()
        self.invocation_id = invocation_id


# session key -> {child_id: _Child}. Keyed by SESSION so abort (which knows
# the session, not the invocation) can kill the whole tree.
_REGISTRY: dict[str, dict[str, _Child]] = {}


def _session_key(ctx: ToolContext) -> str:
    try:
        s = ctx._invocation_context.session
        return f"{s.app_name}/{s.user_id}/{s.id}"
    except Exception:  # noqa: BLE001 — tests with bare contexts
        return "unknown"


def _invocation_id(ctx: ToolContext) -> str:
    try:
        return str(ctx._invocation_context.invocation_id or "")
    except Exception:  # noqa: BLE001
        return ""


def running_children(session_key: str) -> list[dict[str, Any]]:
    """Snapshot for status surfaces: [{id, task, elapsed_s}]."""
    out = []
    for c in (_REGISTRY.get(session_key) or {}).values():
        if not c.atask.done():
            out.append({"id": c.id, "task": c.task_text,
                        "elapsed_s": round(time.perf_counter() - c.started, 1)})
    return out


def cancel_children(session_key: str,
                    invocation_id: Optional[str] = None) -> int:
    """Cancel (and forget) children — the whole session's, or one turn's.

    Called from the invocation-end plugin hook (strays the model chose not to
    collect) and from the broker's abort path (#87: deleting a session must
    stop ALL its work, including nested runs the turn task does not own).
    """
    bucket = _REGISTRY.get(session_key) or {}
    n = 0
    for cid in list(bucket):
        c = bucket[cid]
        if invocation_id and c.invocation_id != invocation_id:
            continue
        if not c.atask.done():
            c.atask.cancel()
            n += 1
        del bucket[cid]
    if not bucket:
        _REGISTRY.pop(session_key, None)
    if n:
        _log.info("subagents: cancelled %d stray explorer(s) for %s", n, session_key)
    return n


# --- envelope (ported, + id) ----------------------------------------------
def enrich_result(
    report: str,
    *,
    id: str,
    task: str,
    agent: str,
    ok: bool = True,
    error: Optional[str] = None,
    elapsed_s: float = 0.0,
    queued_s: float = 0.0,
    tool_calls: int = 0,
    tools_used: Optional[list[str]] = None,
    events: int = 0,
) -> dict[str, Any]:
    """Attributable result envelope. `task` is echoed back because parallel
    results return unordered; `id` ties it to the spawn row in the UI."""
    env: dict[str, Any] = {
        "id": id, "task": task, "agent": agent, "ok": ok,
        "elapsed_s": round(elapsed_s, 3), "queued_s": round(queued_s, 3),
        "tool_calls": tool_calls, "tools_used": tools_used or [],
        "events": events, "report": report,
    }
    if error:
        env["error"] = error
    return env


# --- the nested run (ported core, + state seeding) -------------------------
async def _run_child(agent: Any, task_text: str, ctx: ToolContext,
                     child_id: str) -> dict[str, Any]:
    from google.adk.memory.in_memory_memory_service import InMemoryMemoryService
    from google.adk.runners import Runner
    from google.adk.sessions.in_memory_session_service import InMemorySessionService
    from google.adk.tools._forwarding_artifact_service import (
        ForwardingArtifactService,
    )
    from google.adk.utils.context_utils import Aclosing

    from ..plugins.model_session import STATE_ENDPOINT_KEY, STATE_MODEL_KEY

    t_start = time.perf_counter()
    tool_calls = 0
    tools_used: list[str] = []
    n_events = 0
    last_text = ""
    ok, error = True, None

    async with _sem():
        queued_s = time.perf_counter() - t_start
        try:
            inv = ctx._invocation_context
            plugins = inv.plugin_manager.plugins if inv else []
            # Seed the child session:
            #   * subagent=True — the permission plugin turns any would-ask
            #     into a deny (no human is reachable from here).
            #   * the parent's model pin — the model-session plugin CLEARS the
            #     pin contextvar when a session carries none (measured), so an
            #     unseeded child would silently fall back to the default model.
            seed: dict[str, Any] = {"subagent": True}
            try:
                pstate = inv.session.state
                for k in (STATE_ENDPOINT_KEY, STATE_MODEL_KEY):
                    if pstate.get(k):
                        seed[k] = pstate.get(k)
                # Workspace/backend travel too: read-only tools resolve paths
                # through the same workspace as the parent.
                for k in ("temp:sandbox_backend", "temp:sandbox_workspace"):
                    if pstate.get(k) is not None:
                        seed[k] = pstate.get(k)
            except Exception:  # noqa: BLE001
                pass

            session_service = InMemorySessionService()
            runner = Runner(
                app_name=(inv.app_name if inv else agent.name) or agent.name,
                agent=agent,
                session_service=session_service,
                memory_service=InMemoryMemoryService(),
                artifact_service=ForwardingArtifactService(ctx),
                plugins=list(plugins),
            )
            session = await session_service.create_session(
                app_name=runner.app_name, user_id="subagent", state=seed)
            content = types.Content(
                role="user", parts=[types.Part.from_text(text=task_text)])
            async with Aclosing(runner.run_async(
                    user_id=session.user_id, session_id=session.id,
                    new_message=content)) as agen:
                async for event in agen:
                    n_events += 1
                    for part in (getattr(event.content, "parts", None) or []):
                        fc = getattr(part, "function_call", None)
                        if fc is not None:
                            tool_calls += 1
                            name = getattr(fc, "name", "")
                            if name and name not in tools_used:
                                tools_used.append(name)
                        elif getattr(part, "text", None) and not getattr(
                                part, "thought", False):
                            last_text = part.text
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — a failed child is a RESULT
            ok, error = False, f"{type(e).__name__}: {str(e)[:300]}"

    return enrich_result(
        last_text, id=child_id, task=task_text, agent=getattr(agent, "name", "?"),
        ok=ok and bool(last_text), error=error if error else (
            None if last_text else "explorer returned no report"),
        elapsed_s=time.perf_counter() - t_start, queued_s=queued_s,
        tool_calls=tool_calls, tools_used=tools_used, events=n_events)


# --- the tools --------------------------------------------------------------
class SpawnExplorersTool(BaseTool):
    """Start N read-only explorers in the background; returns ids at once."""

    def __init__(self, agent: Any) -> None:
        super().__init__(
            name="spawn_explorers",
            description=(
                "Spawn read-only explorer sub-agents, ONE per independent "
                "question (codebase or web). They run in PARALLEL in the "
                "background; this returns their ids immediately. You MUST "
                "then call collect_explorers to get their reports — by "
                "default wait for all; if a first batch already answers the "
                "question, you may resume with wait='first_done' and cancel "
                "the rest. Each task string is the explorer's ENTIRE briefing "
                "(it shares no chat history). "
                f"Up to ~{concurrency_limit()} run at once; more just queue."),
        )
        self._agent = agent

    def _get_declaration(self) -> types.FunctionDeclaration:
        return types.FunctionDeclaration(
            name=self.name,
            description=self.description,
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "tasks": types.Schema(
                        type=types.Type.ARRAY,
                        items=types.Schema(type=types.Type.STRING),
                        description="One focused, self-contained question per explorer.",
                    ),
                },
                required=["tasks"],
            ),
        )

    async def run_async(self, *, args: dict[str, Any],
                        tool_context: ToolContext) -> Any:
        tasks = [str(t) for t in (args.get("tasks") or []) if str(t).strip()]
        if not tasks:
            return {"error": "tasks must be a non-empty list of questions"}
        skey = _session_key(tool_context)
        inv_id = _invocation_id(tool_context)
        bucket = _REGISTRY.setdefault(skey, {})
        spawned = []
        for text in tasks:
            cid = f"e{uuid.uuid4().hex[:6]}"
            atask = asyncio.create_task(
                _run_child(self._agent, text, tool_context, cid))
            bucket[cid] = _Child(cid, text, atask, inv_id)
            spawned.append({"id": cid, "task": text})
        _log.info("subagents: spawned %d explorer(s) for %s", len(spawned), skey)
        return {
            "spawned": spawned,
            "running": len(running_children(skey)),
            "next": "call collect_explorers (default waits for all)",
        }


class CollectExplorersTool(BaseTool):
    """Wait for spawned explorers and return their reports."""

    def __init__(self) -> None:
        super().__init__(
            name="collect_explorers",
            description=(
                "Collect reports from spawned explorers. Default waits for "
                "ALL outstanding ones. wait='first_done' returns as soon as "
                "at least one finishes (the rest keep running — collect again "
                "or set cancel_remaining=true once you have enough). Each "
                "report is {id, task, ok, report, elapsed_s, tool_calls, "
                "tools_used, error?}."),
        )

    def _get_declaration(self) -> types.FunctionDeclaration:
        return types.FunctionDeclaration(
            name=self.name,
            description=self.description,
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "ids": types.Schema(
                        type=types.Type.ARRAY,
                        items=types.Schema(type=types.Type.STRING),
                        description="Specific explorer ids; omit for all outstanding.",
                    ),
                    "wait": types.Schema(
                        type=types.Type.STRING,
                        enum=["all", "first_done"],
                        description="all (default) or first_done for early resume.",
                    ),
                    "timeout_s": types.Schema(
                        type=types.Type.NUMBER,
                        description="Optional cap on how long to wait.",
                    ),
                    "cancel_remaining": types.Schema(
                        type=types.Type.BOOLEAN,
                        description="Cancel whatever is still running after this collect.",
                    ),
                },
            ),
        )

    async def run_async(self, *, args: dict[str, Any],
                        tool_context: ToolContext) -> Any:
        skey = _session_key(tool_context)
        bucket = _REGISTRY.get(skey) or {}
        want_ids = [str(i) for i in (args.get("ids") or [])] or list(bucket)
        children = [bucket[i] for i in want_ids if i in bucket]
        if not children:
            return {"done": [], "running": [],
                    "note": "no outstanding explorers (already collected?)"}

        mode = str(args.get("wait") or "all")
        timeout = args.get("timeout_s")
        pending_tasks = {c.atask for c in children if not c.atask.done()}
        if pending_tasks:
            await asyncio.wait(
                pending_tasks,
                timeout=float(timeout) if timeout else None,
                return_when=(asyncio.FIRST_COMPLETED if mode == "first_done"
                             else asyncio.ALL_COMPLETED),
            )

        done, running = [], []
        for c in children:
            if c.atask.done():
                try:
                    done.append(c.atask.result())
                except asyncio.CancelledError:
                    done.append(enrich_result(
                        "", id=c.id, task=c.task_text, agent="explorer",
                        ok=False, error="cancelled"))
                except Exception as e:  # noqa: BLE001
                    done.append(enrich_result(
                        "", id=c.id, task=c.task_text, agent="explorer",
                        ok=False, error=f"{type(e).__name__}: {str(e)[:200]}"))
                bucket.pop(c.id, None)
            else:
                running.append({"id": c.id, "task": c.task_text,
                                "elapsed_s": round(
                                    time.perf_counter() - c.started, 1)})

        if args.get("cancel_remaining") and running:
            for r in running:
                c = bucket.get(r["id"])
                if c is not None:
                    c.atask.cancel()
                    bucket.pop(c.id, None)
            note = f"cancelled {len(running)} still-running explorer(s)"
            running, extra = [], {"note": note}
        else:
            extra = {}
        if not bucket:
            _REGISTRY.pop(skey, None)
        return {"done": done, "running": running, **extra}


# --- lifecycle -------------------------------------------------------------
class SubagentCleanupPlugin(BasePlugin):
    """Strays must not outlive the turn: whatever the coordinator chose not to
    collect is cancelled when the invocation ends. Abort/delete (#87) goes
    through `cancel_children` directly from the broker."""

    def __init__(self, name: str = "adk_cc_subagent_cleanup") -> None:
        super().__init__(name=name)

    async def after_run_callback(self, *, invocation_context: Any) -> None:
        try:
            s = invocation_context.session
            skey = f"{s.app_name}/{s.user_id}/{s.id}"
            inv = str(invocation_context.invocation_id or "")
        except Exception:  # noqa: BLE001
            return None
        cancel_children(skey, invocation_id=inv or None)
        return None
