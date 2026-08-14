from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class GrokExtensions:
    cwd: str | None = None
    session_id: str | None = None
    max_turns: int | None = None
    sandbox: str | None = None
    rules: str | None = None
    always_approve: bool | None = None
    tools_allow: list[str] | None = None
    tools_deny: list[str] | None = None
    permission_mode: str | None = None
    allow: list[str] | None = None
    deny: list[str] | None = None
    reasoning_effort: str | None = None
    worktree: str | bool | None = None
    timeout_sec: int | None = None
    workspace_mode: Literal["read_only", "in_place", "worktree", "temporary_copy"] | None = None
    permission_policy: Literal["always_approve", "ask", "server"] | None = None
    backend: Literal["auto", "acp", "headless"] | None = None
    include_thoughts: bool = False

    @classmethod
    def from_mapping(cls, data: dict[str, Any] | None) -> GrokExtensions:
        if not data:
            return cls()
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        kwargs = {k: v for k, v in data.items() if k in known}
        return cls(**kwargs)


@dataclass
class CreateResponseCommand:
    model: str
    input_text: str
    stream: bool = False
    background: bool = False
    previous_response_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    x_grok: GrokExtensions = field(default_factory=GrokExtensions)
    # resolved server defaults
    default_always_approve: bool = True
    default_timeout_sec: int = 1800
    default_cwd: str = "."
    # auth / tenancy
    actor_id: str | None = None
    actor_type: str | None = None
    actor_max_concurrent: int | None = None
    request_id: str | None = None
