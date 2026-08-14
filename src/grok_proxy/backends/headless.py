from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from grok_proxy.backends.base import (
    BackendCapabilities,
    BackendError,
    BackendEvent,
    BackendSession,
    BackendSessionRequest,
    PermissionDecisionPayload,
    PromptInput,
)
from grok_proxy.grok_runner import GrokRunner, GrokRunOptions
from grok_proxy.runtime.process_manager import ProcessManager

logger = logging.getLogger(__name__)

# Soft cap for streaming stdout accumulation (chars); excess is dropped with a marker.
DEFAULT_MAX_OUTPUT_CHARS = 2_000_000


def _redact_secrets(text: str) -> str:
    """Best-effort stderr/log redaction for common secret patterns."""
    import re

    patterns = [
        (re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*\S+"), r"\1=[REDACTED]"),
        (re.compile(r"(?i)bearer\s+[A-Za-z0-9\-._~+/]+=*"), "Bearer [REDACTED]"),
        (re.compile(r"sk-[A-Za-z0-9]{10,}"), "sk-[REDACTED]"),
        (re.compile(r"xai-[A-Za-z0-9]{10,}"), "xai-[REDACTED]"),
        (re.compile(r"gp_(?:live|test)_[A-Za-z0-9]+"), "gp_[REDACTED]"),
    ]
    out = text
    for cre, repl in patterns:
        out = cre.sub(repl, out)
    return out


def _tool_arguments(raw: dict[str, Any]) -> dict[str, Any]:
    """Pull tool args from the various shapes Grok streaming-json uses."""
    for key in (
        "arguments",
        "input",
        "args",
        "parameters",
        "toolInput",
        "tool_input",
        "rawInput",
    ):
        val = raw.get(key)
        if isinstance(val, dict) and val:
            return val
        if isinstance(val, str) and val.strip():
            try:
                parsed = json.loads(val)
            except json.JSONDecodeError:
                return {"value": val.strip()[:200]}
            if isinstance(parsed, dict) and parsed:
                return parsed
    picked: dict[str, Any] = {}
    for key in (
        "path",
        "file",
        "file_path",
        "target_file",
        "target_directory",
        "command",
        "cmd",
        "pattern",
        "query",
        "url",
    ):
        if raw.get(key):
            picked[key] = raw[key]
    return picked


def map_streaming_event(raw: dict[str, Any]) -> BackendEvent | None:
    """Map Grok streaming-json event dicts to BackendEvent."""
    etype = str(raw.get("type") or "")
    if etype in ("text", "assistant", "message"):
        data = raw.get("data")
        if data is None:
            data = raw.get("text") or raw.get("content") or ""
        return BackendEvent(type="text", data={"text": str(data)}, raw=raw)
    if etype in ("tool_call", "tool-call", "toolCall"):
        return BackendEvent(
            type="tool_call",
            data={
                "id": raw.get("id") or raw.get("toolCallId") or raw.get("call_id"),
                "name": raw.get("name") or raw.get("toolName") or raw.get("tool"),
                "arguments": _tool_arguments(raw),
                "title": raw.get("title") or raw.get("description"),
            },
            raw=raw,
        )
    if etype in ("tool_update", "tool-update", "toolUpdate"):
        return BackendEvent(
            type="tool_update",
            data={
                "id": raw.get("id") or raw.get("toolCallId"),
                "status": raw.get("status"),
                "partial": raw.get("partial") or raw.get("data"),
            },
            raw=raw,
        )
    if etype in ("tool_result", "tool-result", "toolResult"):
        return BackendEvent(
            type="tool_result",
            data={
                "id": raw.get("id") or raw.get("toolCallId"),
                "result": raw.get("result") or raw.get("output") or raw.get("data"),
                "status": raw.get("status") or "completed",
            },
            raw=raw,
        )
    if etype in ("plan", "todo", "todos"):
        return BackendEvent(type="plan", data={"plan": raw.get("plan") or raw.get("data") or raw}, raw=raw)
    if etype == "usage":
        return BackendEvent(type="usage", data={"usage": raw.get("usage") or raw}, raw=raw)
    if etype in ("permission", "permission_request", "permissionRequest"):
        return BackendEvent(
            type="permission_request",
            data={
                "id": raw.get("id") or raw.get("permissionId"),
                "category": raw.get("category") or raw.get("kind") or "unknown",
                "title": raw.get("title") or raw.get("message"),
                "arguments": raw.get("arguments") or raw.get("input") or {},
                "risk": raw.get("risk") or "medium",
            },
            raw=raw,
        )
    if etype == "end":
        return BackendEvent(
            type="end",
            data={
                "session_id": raw.get("sessionId") or raw.get("session_id"),
                "stop_reason": raw.get("stopReason") or raw.get("stop_reason"),
                "num_turns": raw.get("num_turns") or raw.get("numTurns"),
                "usage": raw.get("usage"),
                "text": raw.get("text"),
                "request_id": raw.get("requestId") or raw.get("request_id"),
            },
            raw=raw,
        )
    if etype == "error":
        return BackendEvent(
            type="error",
            data={
                "message": raw.get("message") or raw.get("error") or "unknown error",
                "code": raw.get("code") or "grok_error",
            },
            raw=raw,
        )
    if etype == "thought":
        # Drop raw thoughts by default; orchestrator may log under debug.
        return None
    # Result-style final object sometimes appears as a non-typed payload
    if "text" in raw and etype in ("", "result"):
        return BackendEvent(
            type="end",
            data={
                "session_id": raw.get("sessionId") or raw.get("session_id"),
                "stop_reason": raw.get("stopReason") or raw.get("stop_reason"),
                "num_turns": raw.get("num_turns") or raw.get("numTurns"),
                "usage": raw.get("usage"),
                "text": raw.get("text"),
            },
            raw=raw,
        )
    logger.debug("unmapped headless event type=%s keys=%s", etype, list(raw.keys())[:12])
    return None


@dataclass
class _HeadlessHandle:
    request: BackendSessionRequest
    prompt: str | None = None
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    prompt_file: Path | None = None
    response_id: str | None = None


class HeadlessBackend:
    """Wrap GrokRunner as a GrokBackend (one-shot headless process)."""

    def __init__(
        self,
        runner: GrokRunner | None = None,
        *,
        grok_bin: str = "grok",
        process_manager: ProcessManager | None = None,
        max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
        use_prompt_file: bool = True,
        prompt_file_threshold: int = 0,
    ) -> None:
        self.runner = runner or GrokRunner(grok_bin)
        self.grok_bin = grok_bin
        self.process_manager = process_manager or ProcessManager()
        self.max_output_chars = max_output_chars
        self.use_prompt_file = use_prompt_file
        self.prompt_file_threshold = prompt_file_threshold
        self._caps = BackendCapabilities(
            name="headless",
            supports_permissions=False,
            supports_tools=True,
            supports_plan=True,
            supports_session_resume=True,
            supports_streaming=True,
        )

    @property
    def capabilities(self) -> BackendCapabilities:
        return self._caps

    async def start_session(self, request: BackendSessionRequest) -> BackendSession:
        sid = request.session_id or f"hl_{uuid.uuid4().hex[:16]}"
        handle = _HeadlessHandle(request=request)
        return BackendSession(
            session_id=sid,
            backend_name="headless",
            cwd=request.cwd,
            model=request.model,
            handle=handle,
            metadata={"resume": request.session_id},
        )

    async def send_prompt(self, session: BackendSession, prompt: PromptInput) -> None:
        handle = self._handle(session)
        handle.prompt = prompt.text
        handle.cancel_event.clear()

    async def events(self, session: BackendSession) -> AsyncIterator[BackendEvent]:
        handle = self._handle(session)
        if handle.prompt is None:
            raise BackendError("send_prompt must be called before events()", code="no_prompt")

        req = handle.request
        prompt_path = self._write_prompt_file(handle.prompt)
        handle.prompt_file = prompt_path
        try:
            if not req.always_approve:
                logger.info(
                    "HeadlessBackend forcing always_approve "
                    "(interactive permissions unsupported on grok -p)"
                )
            opts = GrokRunOptions(
                prompt=handle.prompt if not self.use_prompt_file else "",
                model=req.model,
                cwd=req.cwd,
                stream=True,
                session_id=req.session_id,
                always_approve=True,
                max_turns=req.max_turns,
                sandbox=req.sandbox,
                rules=req.rules,
                tools_allow=req.tools_allow,
                tools_deny=req.tools_deny,
                permission_mode=req.permission_mode,
                allow=req.allow,
                deny=req.deny,
                reasoning_effort=req.reasoning_effort,
                worktree=req.worktree,
                timeout_sec=req.timeout_sec,
                grok_bin=self.grok_bin,
                prompt_file=str(prompt_path) if self.use_prompt_file else None,
                track_id=handle.response_id or session.session_id,
            )
            # Ensure runner can register child PIDs for reclaim/cancel
            if getattr(self.runner, "process_manager", None) is None:
                self.runner.process_manager = self.process_manager
            text_acc = 0
            async for raw in self.runner.stream(opts):
                if handle.cancel_event.is_set():
                    yield BackendEvent(
                        type="error",
                        data={"message": "cancelled", "code": "cancelled"},
                    )
                    return
                if not isinstance(raw, dict):
                    continue
                mapped = map_streaming_event(raw)
                if mapped is None:
                    continue
                if mapped.type == "text":
                    chunk = str(mapped.data.get("text") or "")
                    text_acc += len(chunk)
                    if text_acc > self.max_output_chars:
                        yield BackendEvent(
                            type="text",
                            data={"text": "\n[output truncated by gateway]\n", "truncated": True},
                        )
                        continue
                if mapped.type == "error":
                    msg = str(mapped.data.get("message") or "")
                    mapped.data["message"] = _redact_secrets(msg)
                yield mapped
        finally:
            self._cleanup_prompt_file(handle)

    async def resolve_permission(
        self,
        session: BackendSession,
        decision: PermissionDecisionPayload,
    ) -> None:
        # Headless always-approve path has no interactive permissions.
        raise BackendError(
            "HeadlessBackend does not support interactive permission decisions",
            code="permissions_unsupported",
        )

    async def cancel(self, session: BackendSession) -> None:
        handle = self._handle(session)
        handle.cancel_event.set()
        rid = handle.response_id or session.session_id
        await self.process_manager.stop(rid)

    async def close(self, session: BackendSession) -> None:
        handle = self._handle(session)
        handle.cancel_event.set()
        self._cleanup_prompt_file(handle)

    def bind_response_id(self, session: BackendSession, response_id: str) -> None:
        self._handle(session).response_id = response_id

    def _handle(self, session: BackendSession) -> _HeadlessHandle:
        if not isinstance(session.handle, _HeadlessHandle):
            raise BackendError("invalid headless session handle", code="bad_session")
        return session.handle

    def _write_prompt_file(self, prompt: str) -> Path:
        fd, name = tempfile.mkstemp(prefix="grok-proxy-prompt-", suffix=".txt")
        path = Path(name)
        try:
            os.write(fd, prompt.encode("utf-8"))
        finally:
            os.close(fd)
        try:
            os.chmod(path, 0o600)
        except OSError:
            logger.warning("failed to chmod prompt file %s", path)
        return path

    def _cleanup_prompt_file(self, handle: _HeadlessHandle) -> None:
        if handle.prompt_file and handle.prompt_file.exists():
            try:
                handle.prompt_file.unlink()
            except OSError:
                logger.warning("failed to delete prompt file %s", handle.prompt_file)
        handle.prompt_file = None
