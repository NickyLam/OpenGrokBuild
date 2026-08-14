from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

BackendEventType = Literal[
    "text",
    "tool_call",
    "tool_update",
    "tool_result",
    "plan",
    "usage",
    "permission_request",
    "end",
    "error",
]


@dataclass
class BackendCapabilities:
    name: str
    supports_permissions: bool = False
    supports_tools: bool = True
    supports_plan: bool = True
    supports_session_resume: bool = True
    supports_streaming: bool = True


@dataclass
class BackendSessionRequest:
    model: str
    cwd: str
    session_id: str | None = None
    always_approve: bool = True
    max_turns: int | None = None
    sandbox: str | None = None
    rules: str | None = None
    tools_allow: list[str] | None = None
    tools_deny: list[str] | None = None
    permission_mode: str | None = None
    allow: list[str] | None = None
    deny: list[str] | None = None
    reasoning_effort: str | None = None
    worktree: str | bool | None = None
    timeout_sec: int = 1800
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class BackendSession:
    session_id: str
    backend_name: str
    cwd: str
    model: str
    handle: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class PromptInput:
    text: str
    previous_response_id: str | None = None


@dataclass
class PermissionDecisionPayload:
    permission_id: str
    decision: str
    feedback: str | None = None
    scope: dict[str, Any] | None = None


@dataclass
class BackendEvent:
    type: BackendEventType
    data: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] | None = None


class BackendError(Exception):
    def __init__(
        self,
        message: str,
        *,
        code: str = "backend_error",
        retriable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.retriable = retriable
        self.details = details or {}


@runtime_checkable
class GrokBackend(Protocol):
    @property
    def capabilities(self) -> BackendCapabilities: ...

    async def start_session(self, request: BackendSessionRequest) -> BackendSession: ...

    async def send_prompt(self, session: BackendSession, prompt: PromptInput) -> None: ...

    def events(self, session: BackendSession) -> AsyncIterator[BackendEvent]: ...

    async def resolve_permission(
        self,
        session: BackendSession,
        decision: PermissionDecisionPayload,
    ) -> None: ...

    async def cancel(self, session: BackendSession) -> None: ...

    async def close(self, session: BackendSession) -> None: ...
