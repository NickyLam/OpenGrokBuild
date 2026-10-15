"""
ACP (Agent Client Protocol) backend for `grok agent stdio`.

Verified against Grok Build CLI 0.2.x:

1. initialize {protocolVersion:1, clientInfo, capabilities}
2. authenticate {methodId: "cached_token"}
3. session/new {cwd, mcpServers: []}
4. session/prompt {sessionId, prompt: [{type:text, text: ...}]}
5. session/update notifications (agent_message_chunk, agent_thought_chunk, tool_*, …)
6. prompt RPC result {stopReason, _meta.usage}

Cancel: process terminate (no stable cancel RPC in 0.2.x).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from grok_proxy.backends.acp_map import map_session_update, normalize_usage
from grok_proxy.backends.base import (
    BackendCapabilities,
    BackendError,
    BackendEvent,
    BackendSession,
    BackendSessionRequest,
    PermissionDecisionPayload,
    PromptInput,
)
from grok_proxy.backends.headless import HeadlessBackend
from grok_proxy.grok_runner import GrokRunner, iter_ndjson_lines
from grok_proxy.runtime.process_manager import ProcessManager

logger = logging.getLogger(__name__)


@dataclass
class _AcpHandle:
    request: BackendSessionRequest
    proc: asyncio.subprocess.Process | None = None
    prompt: str | None = None
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    rpc_id: int = 0
    pending: dict[int, asyncio.Future[Any]] = field(default_factory=dict)
    event_queue: asyncio.Queue[BackendEvent | None] = field(default_factory=asyncio.Queue)
    reader_task: asyncio.Task[None] | None = None
    stderr_task: asyncio.Task[None] | None = None
    backend_session_id: str | None = None
    text_acc: list[str] = field(default_factory=list)
    prompt_done: asyncio.Event = field(default_factory=asyncio.Event)
    last_usage: dict[str, Any] | None = None
    last_stop_reason: str | None = None
    include_thoughts: bool = False
    stderr_chunks: list[bytes] = field(default_factory=list)
    prompt_rpc_id: int | None = None
    prompt_result_received: bool = False
    eof_error_emitted: bool = False


class AcpBackend:
    """JSON-RPC NDJSON ACP client for `grok agent stdio`."""

    def __init__(
        self,
        *,
        grok_bin: str = "grok",
        process_manager: ProcessManager | None = None,
        agent_args: list[str] | None = None,
        auth_method_id: str = "cached_token",
        include_thoughts: bool = False,
    ) -> None:
        self.grok_bin = grok_bin
        self.process_manager = process_manager or ProcessManager()
        self.agent_args = agent_args or ["agent", "stdio"]
        self.auth_method_id = auth_method_id
        self.include_thoughts = include_thoughts
        self._caps = BackendCapabilities(
            name="acp",
            supports_permissions=True,
            supports_tools=True,
            supports_plan=True,
            supports_session_resume=True,
            supports_streaming=True,
        )

    @property
    def capabilities(self) -> BackendCapabilities:
        return self._caps

    async def start_session(self, request: BackendSessionRequest) -> BackendSession:
        cmd = [self.grok_bin, *self.agent_args]
        from grok_proxy.cli_env import apply_grok_cli_env

        env = apply_grok_cli_env(os.environ.copy())
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=request.cwd,
                env=env,
                start_new_session=(os.name != "nt"),
            )
        except FileNotFoundError as e:
            raise BackendError(f"failed to spawn ACP: {e}", code="acp_not_found") from e

        handle = _AcpHandle(
            request=request,
            proc=proc,
            include_thoughts=self.include_thoughts
            or bool(request.metadata.get("include_thoughts")),
        )
        handle.reader_task = asyncio.create_task(self._read_loop(handle))
        handle.stderr_task = asyncio.create_task(self._drain_stderr(handle))
        local_sid = request.session_id or f"acp_{uuid.uuid4().hex[:12]}"

        try:
            # Cold start: plugins/MCP can delay first RPC well past 15s
            await asyncio.sleep(0.15)
            await self._rpc(
                handle,
                "initialize",
                {
                    "protocolVersion": 1,
                    "clientInfo": {"name": "grok-proxy", "version": "0.2.0"},
                    "capabilities": {},
                },
                timeout=60,
            )
            try:
                await self._rpc(
                    handle,
                    "authenticate",
                    {"methodId": self.auth_method_id},
                    timeout=30,
                )
            except BackendError as e:
                logger.warning("ACP authenticate skipped/failed: %s", e)

            result = await self._rpc(
                handle,
                "session/new",
                {
                    "cwd": request.cwd,
                    "mcpServers": [],
                },
                timeout=90,
            )
            if isinstance(result, dict):
                handle.backend_session_id = (
                    result.get("sessionId") or result.get("session_id") or result.get("id")
                )
            if not handle.backend_session_id and request.session_id:
                # loadSession path if supported
                try:
                    loaded = await self._rpc(
                        handle,
                        "session/load",
                        {"sessionId": request.session_id, "cwd": request.cwd, "mcpServers": []},
                        timeout=30,
                    )
                    if isinstance(loaded, dict):
                        handle.backend_session_id = loaded.get("sessionId") or request.session_id
                except BackendError:
                    handle.backend_session_id = request.session_id
            if not handle.backend_session_id:
                raise BackendError("session/new returned no sessionId", code="acp_no_session")
        except Exception as e:  # noqa: BLE001
            await self._kill(handle)
            raise BackendError(f"ACP handshake failed: {e}", code="acp_handshake_failed") from e

        return BackendSession(
            session_id=str(handle.backend_session_id),
            backend_name="acp",
            cwd=request.cwd,
            model=request.model,
            handle=handle,
            metadata={"local_id": local_sid},
        )

    async def send_prompt(self, session: BackendSession, prompt: PromptInput) -> None:
        handle = self._handle(session)
        handle.prompt = prompt.text
        handle.text_acc.clear()
        handle.prompt_done.clear()
        handle.prompt_rpc_id = None
        handle.prompt_result_received = False
        handle.eof_error_emitted = False
        handle.last_usage = None
        handle.last_stop_reason = None
        # Fire prompt RPC in background; stream events via notifications + result
        asyncio.create_task(self._run_prompt(handle, session.session_id, prompt.text))

    async def _run_prompt(self, handle: _AcpHandle, session_id: str, text: str) -> None:
        try:
            result = await self._rpc(
                handle,
                "session/prompt",
                {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": text}],
                },
                timeout=handle.request.timeout_sec or 1800,
            )
            if isinstance(result, dict):
                handle.last_stop_reason = result.get("stopReason") or result.get("stop_reason")
                meta = result.get("_meta") if isinstance(result.get("_meta"), dict) else {}
                usage = meta.get("usage") if isinstance(meta, dict) else None
                if isinstance(usage, dict):
                    handle.last_usage = normalize_usage(usage)
                # Emit end after result
                await handle.event_queue.put(
                    BackendEvent(
                        type="end",
                        data={
                            "session_id": session_id,
                            "stop_reason": handle.last_stop_reason or "end_turn",
                            "usage": handle.last_usage,
                            "text": "".join(handle.text_acc),
                            "num_turns": (handle.last_usage or {}).get("num_turns")
                            or (meta or {}).get("numTurns")
                            or (meta or {}).get("num_turns"),
                            "request_id": (meta or {}).get("requestId")
                            or (meta or {}).get("request_id"),
                        },
                    )
                )
        except Exception as e:  # noqa: BLE001
            if not handle.cancel.is_set() and not (
                getattr(e, "code", None) == "acp_dead" and handle.eof_error_emitted
            ):
                await handle.event_queue.put(
                    BackendEvent(
                        type="error",
                        data={"message": str(e), "code": getattr(e, "code", "acp_prompt_failed")},
                    )
                )
        finally:
            handle.prompt_done.set()
            await handle.event_queue.put(None)

    async def events(self, session: BackendSession) -> AsyncIterator[BackendEvent]:
        handle = self._handle(session)
        while True:
            if handle.cancel.is_set() and handle.event_queue.empty():
                yield BackendEvent(type="error", data={"message": "cancelled", "code": "cancelled"})
                return
            try:
                item = await asyncio.wait_for(handle.event_queue.get(), timeout=0.5)
            except TimeoutError:
                if handle.proc and handle.proc.returncode is not None and handle.prompt_done.is_set():
                    break
                continue
            if item is None:
                break
            yield item

    async def resolve_permission(
        self,
        session: BackendSession,
        decision: PermissionDecisionPayload,
    ) -> None:
        handle = self._handle(session)
        # Best-effort permission resolution shapes used by ACP variants
        for method, params in [
            (
                "session/request_permission/response",
                {
                    "sessionId": session.session_id,
                    "requestId": decision.permission_id,
                    "outcome": {
                        "outcome": "selected",
                        "optionId": decision.decision,
                    },
                },
            ),
            (
                "session/permission",
                {
                    "sessionId": session.session_id,
                    "permissionId": decision.permission_id,
                    "decision": decision.decision,
                    "feedback": decision.feedback,
                    "scope": decision.scope,
                },
            ),
        ]:
            try:
                await self._rpc(handle, method, params, timeout=15)
                return
            except BackendError:
                continue
        logger.warning("ACP resolve_permission: no compatible method")

    async def cancel(self, session: BackendSession) -> None:
        handle = self._handle(session)
        handle.cancel.set()
        # Best-effort cancel RPC variants, then kill
        for method in ("session/cancel", "cancel", "session/abort"):
            try:
                await self._rpc(
                    handle,
                    method,
                    {"sessionId": session.session_id},
                    timeout=2,
                )
                break
            except BackendError:
                continue
        await self._kill(handle)
        await handle.event_queue.put(
            BackendEvent(type="error", data={"message": "cancelled", "code": "cancelled"})
        )
        await handle.event_queue.put(None)

    async def close(self, session: BackendSession) -> None:
        handle = self._handle(session)
        handle.cancel.set()
        await self._kill(handle)

    async def _rpc(
        self,
        handle: _AcpHandle,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float = 30,
    ) -> Any:
        if handle.proc is None or handle.proc.stdin is None:
            raise BackendError("ACP process not running", code="acp_dead")
        handle.rpc_id += 1
        rid = handle.rpc_id
        msg = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        handle.pending[rid] = fut
        if method == "session/prompt":
            handle.prompt_rpc_id = rid
        line = json.dumps(msg, ensure_ascii=False) + "\n"
        handle.proc.stdin.write(line.encode("utf-8"))
        await handle.proc.stdin.drain()
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except TimeoutError as e:
            handle.pending.pop(rid, None)
            raise BackendError(f"ACP RPC timeout: {method}", code="acp_timeout") from e

    async def _drain_stderr(self, handle: _AcpHandle) -> None:
        """Must drain stderr or a chatty agent can block on a full pipe."""
        assert handle.proc and handle.proc.stderr
        try:
            while True:
                chunk = await handle.proc.stderr.read(8192)
                if not chunk:
                    break
                handle.stderr_chunks.append(chunk)
                # keep a bounded tail
                if sum(len(c) for c in handle.stderr_chunks) > 200_000:
                    handle.stderr_chunks = handle.stderr_chunks[-20:]
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.debug("ACP stderr drain ended", exc_info=True)

    async def _read_loop(self, handle: _AcpHandle) -> None:
        assert handle.proc and handle.proc.stdout
        try:
            # Robust line reader: oversized NDJSON lines (huge tool results)
            # are dropped with a warning instead of raising LimitOverrunError.
            async for line_b in iter_ndjson_lines(handle.proc.stdout):
                line = line_b.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug("ACP non-json line: %s", line[:120])
                    continue
                if not isinstance(msg, dict):
                    continue
                if "id" in msg and ("result" in msg or "error" in msg):
                    fut = handle.pending.pop(msg["id"], None)
                    if msg["id"] == handle.prompt_rpc_id:
                        handle.prompt_result_received = True
                    if fut and not fut.done():
                        if "error" in msg:
                            err = msg["error"]
                            fut.set_exception(
                                BackendError(
                                    str(err.get("message") if isinstance(err, dict) else err),
                                    code="acp_rpc_error",
                                    details={"error": err},
                                )
                            )
                        else:
                            fut.set_result(msg.get("result"))
                    continue
                method = str(msg.get("method") or "")
                raw_params = msg.get("params")
                params = raw_params if isinstance(raw_params, dict) else {}
                mapped = self._map_notification(method, params, handle)
                if mapped:
                    if mapped.type == "text":
                        handle.text_acc.append(str(mapped.data.get("text") or ""))
                    await handle.event_queue.put(mapped)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("ACP read loop failed")
            tail = b"".join(handle.stderr_chunks).decode(errors="replace")[-800:]
            if tail:
                logger.error("ACP stderr tail: %s", tail)
        finally:
            for fut in handle.pending.values():
                if not fut.done():
                    fut.set_exception(BackendError("ACP process ended", code="acp_dead"))
            handle.pending.clear()
            prompt_active = (
                handle.prompt is not None
                and not handle.prompt_done.is_set()
                and not handle.prompt_result_received
                and not handle.cancel.is_set()
            )
            if prompt_active:
                handle.eof_error_emitted = True
                await handle.event_queue.put(
                    BackendEvent(
                        type="error",
                        data={
                            "message": "ACP process ended before prompt completed",
                            "code": "acp_dead",
                        },
                    )
                )
            # The prompt task emits the terminal sentinel after it has turned
            # the RPC result/error into an end/error event.  This avoids EOF
            # racing ahead of the prompt result on a short-lived ACP process.
            if handle.prompt is None or handle.prompt_done.is_set():
                await handle.event_queue.put(None)

    def _map_notification(
        self,
        method: str,
        params: dict[str, Any],
        handle: _AcpHandle,
    ) -> BackendEvent | None:
        if method == "session/update":
            return map_session_update(params, include_thoughts=handle.include_thoughts)
        if method in ("session/request_permission", "request_permission"):
            return BackendEvent(
                type="permission_request",
                data={
                    "id": params.get("requestId") or params.get("id"),
                    "category": params.get("category") or "unknown",
                    "title": params.get("title") or "Permission required",
                    "arguments": params.get("params") or params.get("arguments") or {},
                    "risk": params.get("risk") or "medium",
                    "options": params.get("options") or [],
                },
                raw=params,
            )
        return None

    async def _kill(self, handle: _AcpHandle) -> None:
        handle.cancel.set()
        for task_attr in ("reader_task", "stderr_task"):
            task = getattr(handle, task_attr, None)
            if task:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
                setattr(handle, task_attr, None)
        if handle.proc and handle.proc.returncode is None:
            try:
                if os.name != "nt" and handle.proc.pid:
                    try:
                        os.killpg(handle.proc.pid, 15)
                    except (ProcessLookupError, PermissionError, OSError):
                        handle.proc.terminate()
                else:
                    handle.proc.terminate()
                await asyncio.wait_for(handle.proc.wait(), timeout=3)
            except Exception:  # noqa: BLE001
                try:
                    handle.proc.kill()
                except Exception:  # noqa: BLE001
                    pass

    def _handle(self, session: BackendSession) -> _AcpHandle:
        if not isinstance(session.handle, _AcpHandle):
            raise BackendError("invalid acp session", code="bad_session")
        return session.handle


def select_backend(
    name: str,
    *,
    grok_bin: str,
    runner: GrokRunner | None = None,
    process_manager: ProcessManager | None = None,
    prefer_acp_if_available: bool = False,
    probe_acp: bool = True,
) -> HeadlessBackend | AcpBackend | Any:
    """Select backend implementation.

    - headless: GrokRunner only
    - acp: force ACP
    - auto: ACP primary with headless failover when agent CLI looks available
    """
    from grok_proxy.backends.failover import FailoverBackend, probe_acp_available

    key = (name or "headless").lower()
    pm = process_manager or ProcessManager()
    headless = HeadlessBackend(
        runner=runner or GrokRunner(grok_bin),
        grok_bin=grok_bin,
        process_manager=pm,
    )
    if key == "acp":
        return AcpBackend(grok_bin=grok_bin, process_manager=pm)
    if key == "auto" or prefer_acp_if_available:
        available = True
        if probe_acp:
            try:
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None
                if loop and loop.is_running():
                    available = True
                else:
                    available = asyncio.run(probe_acp_available(grok_bin))
            except Exception:  # noqa: BLE001
                available = False
        if not available:
            logger.warning("ACP not available for grok_bin=%s; using headless", grok_bin)
            return headless
        logger.info("backend=auto using ACP with headless failover")
        return FailoverBackend(
            primary=AcpBackend(grok_bin=grok_bin, process_manager=pm),
            fallback=headless,
        )
    return headless
