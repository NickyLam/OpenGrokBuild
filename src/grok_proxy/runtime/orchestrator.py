from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from grok_proxy.backends.base import (
    BackendEvent,
    BackendSession,
    BackendSessionRequest,
    GrokBackend,
    PermissionDecisionPayload,
    PromptInput,
)
from grok_proxy.concurrency import ConcurrencyGate, PerKeyConcurrencyTracker
from grok_proxy.errors import ProxyError
from grok_proxy.permissions.broker import PermissionBroker
from grok_proxy.runtime.commands import CreateResponseCommand
from grok_proxy.runtime.process_manager import ProcessManager
from grok_proxy.runtime.state_machine import ResponseStateMachine, ResponseStatus
from grok_proxy.storage.database import Database, new_id
from grok_proxy.storage.models import EventRecord, ResponseRecord
from grok_proxy.workspace.manager import WorkspaceAllocation, WorkspaceManager

logger = logging.getLogger(__name__)


@dataclass
class _LiveRun:
    response_id: str
    task: asyncio.Task[None] | None = None
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    session: BackendSession | None = None
    workspace: WorkspaceAllocation | None = None
    subscribers: list[asyncio.Queue[EventRecord | None]] = field(default_factory=list)
    permission_waiters: dict[str, asyncio.Future[Any]] = field(default_factory=dict)
    # Active response-level timeout; paused while waiting for human approval
    timeout_ctx: asyncio.Timeout | None = None


class ResponseOrchestrator:
    def __init__(
        self,
        db: Database,
        backend: GrokBackend,
        *,
        workspace: WorkspaceManager | None = None,
        permissions: PermissionBroker | None = None,
        process_manager: ProcessManager | None = None,
        max_concurrent: int = 10,
        gate: ConcurrencyGate | None = None,
        per_key_gate: PerKeyConcurrencyTracker | None = None,
    ) -> None:
        self.db = db
        self.backend = backend
        self.workspace = workspace or WorkspaceManager()
        self.permissions = permissions or PermissionBroker(db)
        self.process_manager = process_manager or ProcessManager()
        self.max_concurrent = max_concurrent
        self.gate = gate
        self.per_key_gate = per_key_gate
        self._runs: dict[str, _LiveRun] = {}
        self._lock = asyncio.Lock()

    async def create(self, command: CreateResponseCommand) -> ResponseRecord:
        response_id = new_id("resp")
        xg = command.x_grok
        cwd = xg.cwd or command.default_cwd
        always_approve = (
            xg.always_approve
            if xg.always_approve is not None
            else command.default_always_approve
        )
        if xg.permission_policy == "ask":
            always_approve = False
        elif xg.permission_policy == "always_approve":
            always_approve = True

        timeout = xg.timeout_sec if xg.timeout_sec is not None else command.default_timeout_sec
        workspace_mode = xg.workspace_mode or "read_only"
        backend_name = getattr(self.backend, "capabilities", None)
        backend_label = backend_name.name if backend_name else "unknown"

        # Pre-check cwd / allocate later at start (need response_id for locks)
        self.workspace.resolve_and_check(cwd)

        meta = dict(command.metadata)
        if command.actor_id:
            meta.setdefault("actor_id", command.actor_id)
        if command.actor_type:
            meta.setdefault("actor_type", command.actor_type)
        if command.request_id:
            meta.setdefault("request_id", command.request_id)
        if command.actor_max_concurrent is not None:
            meta["actor_max_concurrent"] = command.actor_max_concurrent

        now = time.time()
        record = ResponseRecord(
            id=response_id,
            status=ResponseStatus.QUEUED.value,
            model=command.model,
            backend=backend_label,
            input_json={"input": command.input_text, "previous_response_id": command.previous_response_id},
            metadata_json=meta,
            x_grok_json={
                "cwd": cwd,
                "session_id": xg.session_id,
                "max_turns": xg.max_turns,
                "sandbox": xg.sandbox,
                "rules": xg.rules,
                "always_approve": always_approve,
                "tools_allow": xg.tools_allow,
                "tools_deny": xg.tools_deny,
                "permission_mode": xg.permission_mode,
                "allow": xg.allow,
                "deny": xg.deny,
                "reasoning_effort": xg.reasoning_effort,
                "worktree": xg.worktree,
                "timeout_sec": timeout,
                "workspace_mode": workspace_mode,
                "permission_policy": xg.permission_policy,
                "stream": command.stream,
                "background": command.background,
                "include_thoughts": xg.include_thoughts,
            },
            session_id=xg.session_id,
            source_cwd=str(self.workspace.resolve_and_check(cwd)),
            run_cwd=None,
            workspace_mode=workspace_mode,
            created_at=now,
        )
        await self.db.create_response_async(record)
        await self._emit(response_id, "response.created", {"id": response_id, "status": "queued"})
        await self._emit(response_id, "response.queued", {"id": response_id})
        live = _LiveRun(response_id=response_id)
        self._runs[response_id] = live

        if not command.background:
            await self.start(response_id)
            if not command.stream:
                await self._wait_terminal(response_id, timeout=timeout + 30)
        else:
            # fire-and-forget background
            asyncio.create_task(self.start(response_id))

        return self.get(response_id)

    async def start(self, response_id: str) -> None:
        record = self.get(response_id)
        sm = ResponseStateMachine(record.status)
        if sm.is_terminal:
            return
        live = self._runs.setdefault(response_id, _LiveRun(response_id=response_id))
        if live.task and not live.task.done():
            return

        async def _runner() -> None:
            rec0 = await self.get_async(response_id)
            actor_id = str((rec0.metadata_json or {}).get("actor_id") or "")
            actor_limit = (rec0.metadata_json or {}).get("actor_max_concurrent")
            try:
                actor_limit_i = int(actor_limit) if actor_limit is not None else None
            except (TypeError, ValueError):
                actor_limit_i = None

            # Global gate first, then per-key (both fail-fast)
            if self.gate is not None:
                await self.gate.acquire()
            acquired_key = False
            try:
                if self.per_key_gate is not None and actor_id:
                    await self.per_key_gate.acquire(actor_id, actor_limit_i)
                    acquired_key = True
                logger.info(
                    "response.start id=%s actor=%s request_id=%s",
                    response_id,
                    actor_id or "-",
                    (rec0.metadata_json or {}).get("request_id") or "-",
                )
                await self._execute(response_id)
            except ProxyError as e:
                # Mark failed if we never entered _execute (e.g. key concurrency / global gate)
                rec_f = await self.get_async(response_id)
                if rec_f.status in (ResponseStatus.QUEUED.value, ResponseStatus.IN_PROGRESS.value) and e.code in (
                    "key_max_concurrent",
                    "max_concurrent",
                ):
                    rec_f.status = ResponseStatus.FAILED.value
                    rec_f.error_code = e.code
                    rec_f.error_message = e.message
                    rec_f.completed_at = time.time()
                    await self.db.update_response_async(rec_f)
                    await self._emit(
                        response_id,
                        "response.failed",
                        {"id": response_id, "code": e.code, "message": e.message},
                    )
                    self._notify_terminal(response_id)
                    self._finalize_run(response_id)
                elif e.code not in ("key_max_concurrent", "max_concurrent"):
                    raise
            finally:
                if acquired_key and self.per_key_gate is not None and actor_id:
                    await self.per_key_gate.release(actor_id, actor_limit_i)
                if self.gate is not None:
                    await self.gate.release()

        live.task = asyncio.create_task(_runner())

    def get(self, response_id: str) -> ResponseRecord:
        rec = self.db.get_response(response_id)
        if rec is None:
            raise ProxyError("response not found", status_code=404, code="response_not_found")
        return rec

    async def get_async(self, response_id: str) -> ResponseRecord:
        rec = await self.db.get_response_async(response_id)
        if rec is None:
            raise ProxyError("response not found", status_code=404, code="response_not_found")
        return rec

    async def cancel(self, response_id: str, actor: str = "api") -> ResponseRecord:
        record = await self.get_async(response_id)
        sm = ResponseStateMachine(record.status)
        if sm.is_terminal:
            if record.status == ResponseStatus.CANCELLED.value:
                return record
            # already terminal non-cancelled: cancel is no-op for status
            return record

        live = self._runs.get(response_id)
        if live:
            live.cancel_event.set()
            if live.session is not None:
                try:
                    await self.backend.cancel(live.session)
                except Exception:  # noqa: BLE001
                    logger.exception("backend cancel failed for %s", response_id)
            # unblock permission waiters
            for fut in list(live.permission_waiters.values()):
                if not fut.done():
                    fut.cancel()

        try:
            sm.transition(ResponseStatus.CANCELLED)
        except ProxyError:
            return await self.get_async(response_id)

        now = time.time()
        record = await self.get_async(response_id)
        record.status = ResponseStatus.CANCELLED.value
        record.cancelled_at = now
        record.completed_at = now
        await self.db.update_response_async(record)
        await self._emit(response_id, "response.cancelled", {"id": response_id})
        self.db.insert_audit(
            actor_type="user",
            actor_id=actor,
            action="response.cancelled",
            resource_type="response",
            resource_id=response_id,
            payload={},
        )
        await self._cleanup_run(response_id, keep_workspace=False)
        self._notify_terminal(response_id)
        # If no task is running the _execute finally-block will never fire;
        # drop the live entry here so _runs does not leak.
        if live is None or live.task is None or live.task.done():
            self._finalize_run(response_id)
        return await self.get_async(response_id)

    async def stream_events(
        self,
        response_id: str,
        after_sequence: int = 0,
    ) -> AsyncIterator[EventRecord]:
        await self.get_async(response_id)  # 404 if missing
        # replay first
        for ev in await self.db.list_events_async(response_id, after_sequence=after_sequence):
            yield ev
            after_sequence = max(after_sequence, ev.sequence_number)

        record = await self.get_async(response_id)
        if record.status in {
            ResponseStatus.COMPLETED.value,
            ResponseStatus.FAILED.value,
            ResponseStatus.CANCELLED.value,
            ResponseStatus.INCOMPLETE.value,
        }:
            return

        live = self._runs.setdefault(response_id, _LiveRun(response_id=response_id))
        queue: asyncio.Queue[EventRecord | None] = asyncio.Queue()
        live.subscribers.append(queue)
        try:
            while True:
                item = await queue.get()
                if item is None:
                    # terminal signal — drain remaining from DB
                    for ev in await self.db.list_events_async(
                        response_id, after_sequence=after_sequence
                    ):
                        yield ev
                    return
                if item.sequence_number <= after_sequence:
                    continue
                yield item
                after_sequence = item.sequence_number
                if item.event_type in {
                    "response.completed",
                    "response.failed",
                    "response.cancelled",
                    "response.incomplete",
                }:
                    return
        finally:
            if queue in live.subscribers:
                live.subscribers.remove(queue)

    async def decide_permission(
        self,
        permission_id: str,
        *,
        decision: str,
        actor_id: str = "api",
        feedback: str | None = None,
        scope: dict[str, Any] | None = None,
    ) -> Any:
        rec = self.permissions.decide(
            permission_id,
            decision=decision,
            actor_id=actor_id,
            feedback=feedback,
            scope=scope,
        )
        await self._emit(
            rec.response_id,
            "response.permission.resolved",
            {
                "permission_id": rec.id,
                "decision": rec.decision,
                "feedback": rec.feedback,
            },
        )
        live = self._runs.get(rec.response_id)
        if live and rec.id in live.permission_waiters:
            fut = live.permission_waiters[rec.id]
            if not fut.done():
                fut.set_result(rec)

        if decision == "cancel_run":
            await self.cancel(rec.response_id, actor=actor_id)
            return rec

        # Resume backend if waiting
        record = await self.get_async(rec.response_id)
        if record.status == ResponseStatus.WAITING_FOR_APPROVAL.value:
            record.status = ResponseStatus.IN_PROGRESS.value
            await self.db.update_response_async(record)
            await self._emit(rec.response_id, "response.in_progress", {"id": rec.response_id, "resumed": True})

        if live and live.session is not None:
            try:
                await self.backend.resolve_permission(
                    live.session,
                    PermissionDecisionPayload(
                        permission_id=rec.id,
                        decision=decision,
                        feedback=feedback,
                        scope=scope,
                    ),
                )
            except Exception:  # noqa: BLE001
                logger.exception("resolve_permission failed")
        return rec

    async def shutdown(self) -> None:
        for rid in list(self._runs):
            try:
                await self.cancel(rid, actor="shutdown")
            except Exception:  # noqa: BLE001
                logger.exception("shutdown cancel failed for %s", rid)
        await self.process_manager.stop_all(force=True)

    # ---- internals ----

    async def _execute(self, response_id: str) -> None:
        live = self._runs.setdefault(response_id, _LiveRun(response_id=response_id))
        record = await self.get_async(response_id)
        xg = record.x_grok_json
        timeout = int(xg.get("timeout_sec") or 600)

        try:
            sm = ResponseStateMachine(record.status)
            sm.transition(ResponseStatus.IN_PROGRESS)
            record.status = ResponseStatus.IN_PROGRESS.value
            record.started_at = time.time()
            if not await self.db.update_response_if_active_async(record):
                return  # already terminal (e.g. cancelled before start)
            await self._emit(response_id, "response.in_progress", {"id": response_id})

            alloc = self.workspace.allocate(
                str(xg.get("cwd") or record.source_cwd),
                mode=xg.get("workspace_mode"),
                response_id=response_id,
                worktree=xg.get("worktree"),
            )
            live.workspace = alloc
            record.run_cwd = alloc.run_cwd
            record.workspace_mode = alloc.mode
            record.source_cwd = alloc.source_cwd
            # Conditional write: a concurrent cancel() may have gone terminal
            # while allocate yielded — never clobber a terminal status.
            if not await self.db.update_response_if_active_async(record):
                return
            if live.cancel_event.is_set():
                return
            if alloc.mode == "worktree":
                await self._emit(
                    response_id,
                    "response.workspace.created",
                    {
                        "source_cwd": alloc.source_cwd,
                        "run_cwd": alloc.run_cwd,
                        "mode": alloc.mode,
                    },
                )

            req = BackendSessionRequest(
                model=record.model,
                cwd=alloc.run_cwd,
                session_id=xg.get("session_id") or record.session_id,
                always_approve=bool(xg.get("always_approve", True)),
                max_turns=xg.get("max_turns"),
                sandbox=xg.get("sandbox"),
                rules=xg.get("rules"),
                tools_allow=xg.get("tools_allow"),
                tools_deny=xg.get("tools_deny"),
                permission_mode=xg.get("permission_mode"),
                allow=xg.get("allow"),
                deny=xg.get("deny"),
                reasoning_effort=xg.get("reasoning_effort"),
                worktree=None if alloc.mode == "worktree" else xg.get("worktree"),
                timeout_sec=timeout,
                metadata={"response_id": response_id},
            )
            session = await self.backend.start_session(req)
            live.session = session
            bind = getattr(self.backend, "bind_response_id", None)
            if callable(bind):
                bind(session, response_id)

            input_text = str(record.input_json.get("input") or "")
            await self.backend.send_prompt(session, PromptInput(text=input_text))

            text_parts: list[str] = []
            usage: dict[str, Any] | None = None
            end_data: dict[str, Any] = {}
            saw_end = False

            async def _consume() -> None:
                nonlocal usage, end_data, saw_end
                async for be in self.backend.events(session):
                    if live.cancel_event.is_set():
                        break
                    await self._handle_backend_event(
                        response_id,
                        be,
                        text_parts=text_parts,
                    )
                    if be.type == "usage" and isinstance(be.data.get("usage"), dict):
                        usage = be.data["usage"]
                    if be.type == "end":
                        saw_end = True
                        end_data = be.data
                        if be.data.get("text") and not text_parts:
                            text_parts.append(str(be.data["text"]))
                        if isinstance(be.data.get("usage"), dict):
                            usage = be.data["usage"]
                    if be.type == "error":
                        code = str(be.data.get("code") or "backend_error")
                        if code == "cancelled" or live.cancel_event.is_set():
                            return
                        raise ProxyError(
                            str(be.data.get("message") or "backend error"),
                            status_code=502,
                            code=code,
                        )

            try:
                # asyncio.timeout (instead of wait_for) so permission waits can
                # pause the clock — approval time no longer counts against the
                # response timeout (see _handle_permission_request).
                try:
                    async with asyncio.timeout(timeout) as tm:
                        live.timeout_ctx = tm
                        await _consume()
                finally:
                    live.timeout_ctx = None
            except TimeoutError:
                await self.backend.cancel(session)
                record = await self.get_async(response_id)
                if record.status not in {
                    ResponseStatus.CANCELLED.value,
                    ResponseStatus.COMPLETED.value,
                }:
                    record.status = ResponseStatus.INCOMPLETE.value
                    record.error_code = "timeout"
                    record.error_message = f"timed out after {timeout}s"
                    record.text = "".join(text_parts)
                    record.completed_at = time.time()
                    if usage:
                        record.usage_json = usage
                    await self.db.update_response_async(record)
                    await self._emit(
                        response_id,
                        "response.incomplete",
                        {"id": response_id, "reason": "timeout"},
                    )
                return

            # final status if not already terminal (cancel/permission path)
            record = await self.get_async(response_id)
            if record.status in {
                ResponseStatus.CANCELLED.value,
                ResponseStatus.FAILED.value,
                ResponseStatus.INCOMPLETE.value,
                ResponseStatus.COMPLETED.value,
            }:
                return
            if not saw_end:
                raise ProxyError(
                    "backend ended without a final event",
                    status_code=502,
                    code="backend_no_final_event",
                )

            final_text = "".join(text_parts) or str(end_data.get("text") or "")
            record.text = final_text
            record.session_id = (
                end_data.get("session_id") or session.session_id or record.session_id
            )
            if usage:
                record.usage_json = usage
            record.output_json = [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": final_text}],
                }
            ]
            record.status = ResponseStatus.COMPLETED.value
            record.completed_at = time.time()
            await self.db.update_response_async(record)
            await self._emit(
                response_id,
                "response.output_text.done",
                {"text": final_text},
            )
            await self._emit(
                response_id,
                "response.completed",
                {
                    "id": response_id,
                    "session_id": record.session_id,
                    "usage": usage,
                },
            )
        except ProxyError as e:
            record = await self.get_async(response_id)
            if record.status != ResponseStatus.CANCELLED.value:
                record.status = ResponseStatus.FAILED.value
                record.error_code = e.code
                record.error_message = e.message
                record.completed_at = time.time()
                await self.db.update_response_async(record)
                await self._emit(
                    response_id,
                    "response.failed",
                    {"id": response_id, "code": e.code, "message": e.message},
                )
        except Exception:  # noqa: BLE001
            logger.exception("response %s failed", response_id)
            record = await self.get_async(response_id)
            if record.status != ResponseStatus.CANCELLED.value:
                # Never surface raw internal exception text to clients (may leak
                # paths/config); details are in the server log above.
                message = "internal error (see server logs)"
                record.status = ResponseStatus.FAILED.value
                record.error_code = "internal_error"
                record.error_message = message
                record.completed_at = time.time()
                await self.db.update_response_async(record)
                await self._emit(
                    response_id,
                    "response.failed",
                    {"id": response_id, "code": "internal_error", "message": message},
                )
        finally:
            try:
                if live.session is not None:
                    await self.backend.close(live.session)
            except Exception:  # noqa: BLE001
                logger.exception("backend close failed")
            await self._cleanup_run(response_id, keep_workspace=False)
            self._notify_terminal(response_id)
            self._finalize_run(response_id)

    async def _handle_backend_event(
        self,
        response_id: str,
        be: BackendEvent,
        *,
        text_parts: list[str],
    ) -> None:
        if be.type == "text":
            chunk = str(be.data.get("text") or "")
            text_parts.append(chunk)
            await self._emit(response_id, "response.output_text.delta", {"delta": chunk})
        elif be.type == "tool_call":
            await self._emit(
                response_id,
                "response.tool_call.started",
                {
                    "id": be.data.get("id"),
                    "name": be.data.get("name"),
                    "arguments": be.data.get("arguments"),
                    "title": be.data.get("title"),
                },
            )
        elif be.type == "tool_update":
            await self._emit(response_id, "response.tool_call.updated", be.data)
        elif be.type == "tool_result":
            status = str(be.data.get("status") or "completed")
            et = (
                "response.tool_call.failed"
                if status in ("failed", "error")
                else "response.tool_call.completed"
            )
            await self._emit(response_id, et, be.data)
        elif be.type == "plan":
            await self._emit(response_id, "response.plan.updated", be.data)
        elif be.type == "usage":
            await self._emit(response_id, "response.usage.updated", be.data)
        elif be.type == "permission_request":
            await self._handle_permission_request(response_id, be)
        elif be.type == "error":
            # handled by caller for terminal errors; still journal
            await self._emit(response_id, "response.failed", be.data)
        elif be.type == "end":
            # end is aggregated by caller
            pass

    async def _handle_permission_request(self, response_id: str, be: BackendEvent) -> None:
        category = str(be.data.get("category") or "unknown")
        risk = str(be.data.get("risk") or "medium")
        arguments = be.data.get("arguments") if isinstance(be.data.get("arguments"), dict) else {}
        evaluation = self.permissions.evaluate(
            category=category,
            risk=risk,
            arguments=arguments,
            force_ask=False,
        )
        if evaluation.action == "allow":
            live = self._runs.get(response_id)
            if live and live.session is not None:
                await self.backend.resolve_permission(
                    live.session,
                    PermissionDecisionPayload(
                        permission_id=str(be.data.get("id") or "auto"),
                        decision="allow_once",
                    ),
                )
            await self._emit(
                response_id,
                "response.permission.resolved",
                {"decision": "allow_once", "auto": True, "category": category},
            )
            return
        if evaluation.action == "deny":
            live = self._runs.get(response_id)
            if live and live.session is not None:
                await self.backend.resolve_permission(
                    live.session,
                    PermissionDecisionPayload(
                        permission_id=str(be.data.get("id") or "auto"),
                        decision="deny_once",
                        feedback="Denied by server policy",
                    ),
                )
            await self._emit(
                response_id,
                "response.permission.resolved",
                {"decision": "deny_once", "auto": True, "category": category},
            )
            return

        perm = self.permissions.create_pending(
            response_id=response_id,
            category=category,
            risk=risk,
            arguments=arguments or {},
            tool_call_id=str(be.data.get("id")) if be.data.get("id") else None,
            title=str(be.data.get("title") or "Permission required"),
            description=str(be.data.get("description") or ""),
        )
        record = await self.get_async(response_id)
        record.status = ResponseStatus.WAITING_FOR_APPROVAL.value
        await self.db.update_response_async(record)
        await self._emit(
            response_id,
            "response.permission.required",
            {
                "permission_id": perm.id,
                "category": category,
                "risk": risk,
                "arguments": arguments,
                "title": perm.title,
                "options": perm.options_json,
                "expires_at": perm.expires_at,
            },
        )
        live = self._runs.setdefault(response_id, _LiveRun(response_id=response_id))
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        live.permission_waiters[perm.id] = fut
        # Pause the response-level timeout while a human decides; approval
        # latency is governed solely by permission_timeout_sec.
        tm = live.timeout_ctx
        resume_after: float | None = None
        if tm is not None:
            deadline = tm.when()
            if deadline is not None:
                resume_after = max(0.0, deadline - loop.time())
                tm.reschedule(None)
        try:
            await asyncio.wait_for(fut, timeout=self.permissions.permission_timeout_sec)
        except TimeoutError:
            rec = self.permissions.get(perm.id)
            if rec.status == "pending":
                rec.status = "expired"
                rec.decision = "expired"
                rec.decided_at = time.time()
                self.db.update_permission(rec)
            raise ProxyError(
                "permission timed out", status_code=408, code="permission_timeout"
            ) from None
        finally:
            live.permission_waiters.pop(perm.id, None)
            if tm is not None and resume_after is not None:
                tm.reschedule(loop.time() + resume_after)

    async def _emit(self, response_id: str, event_type: str, payload: dict[str, Any]) -> EventRecord:
        ev = await self.db.append_event_async(response_id, event_type, payload)
        live = self._runs.get(response_id)
        if live:
            for q in list(live.subscribers):
                try:
                    q.put_nowait(ev)
                except Exception:  # noqa: BLE001
                    pass
        return ev

    def _notify_terminal(self, response_id: str) -> None:
        live = self._runs.get(response_id)
        if not live:
            return
        for q in list(live.subscribers):
            try:
                q.put_nowait(None)
            except Exception:  # noqa: BLE001
                pass

    def _finalize_run(self, response_id: str) -> None:
        """Drop the live entry once terminal (subscribers already got the sentinel).

        Without this _runs grows forever; terminal responses replay from the DB
        so a live entry is no longer needed.
        """
        self._runs.pop(response_id, None)

    async def _cleanup_run(self, response_id: str, *, keep_workspace: bool) -> None:
        live = self._runs.get(response_id)
        if not live:
            return
        # Claim the allocation before any await: cancel() and _execute's finally
        # can run concurrently, and the loser must see None and no-op.
        alloc = live.workspace
        if alloc is None:
            return
        live.workspace = None
        self.workspace.release(alloc, response_id)
        if not keep_workspace:
            failed = False
            try:
                rec = await self.get_async(response_id)
                failed = rec.status == ResponseStatus.FAILED.value
            except ProxyError:
                failed = True
            self.workspace.cleanup(alloc, keep_on_failure=failed)

    async def _wait_terminal(self, response_id: str, *, timeout: float) -> None:
        live = self._runs.get(response_id)
        if live and live.task:
            try:
                await asyncio.wait_for(asyncio.shield(live.task), timeout=timeout)
            except TimeoutError:
                await self.cancel(response_id, actor="wait_timeout")
            except asyncio.CancelledError:
                pass
        # Task completion implies the terminal status was persisted inside
        # _execute's finally block — no polling needed.
