from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from grok_proxy.errors import timeout_client_notice
from grok_proxy.grok_runner import map_usage

# Chat clients (WorkBuddy) only render delta.content. Map Grok tool events
# to compact Markdown so a coding-agent run is not one unformatted blob.
_TOOL_LABELS: dict[str, str] = {
    "read_file": "读文件",
    "read": "读文件",
    "list_dir": "列出目录",
    "ls": "列出目录",
    "grep": "搜索",
    "search_replace": "改文件",
    "str_replace": "改文件",
    "write": "写文件",
    "write_file": "写文件",
    "run_terminal_command": "运行",
    "bash": "运行",
    "shell": "运行",
    "todo_write": "待办",
    "spawn_subagent": "子任务",
    "get_command_or_subagent_output": "查子任务",
    "ask_user_question": "提问",
    "search_tool": "搜工具",
}

_TARGET_KEYS = (
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
    "id",
)


def new_completion_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex}"


def _coerce_tool_args(raw: object) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {"value": raw.strip()[:200]}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _tool_target(args: dict[str, Any]) -> str:
    for key in _TARGET_KEYS:
        val = args.get(key)
        if val is None or val == "":
            continue
        text = " ".join(str(val).split())
        if len(text) > 120:
            text = text[:117] + "..."
        return text
    for val in args.values():
        if isinstance(val, str) and val.strip() and len(val) < 160:
            return " ".join(val.split())
    return ""


def format_tool_markdown(payload: dict[str, Any]) -> tuple[str, str]:
    """Return (label, target) for one tool_call payload."""
    name = str(payload.get("name") or payload.get("title") or "tool").strip() or "tool"
    label = _TOOL_LABELS.get(name, name)
    args = _coerce_tool_args(payload.get("arguments"))
    if not args:
        args = _coerce_tool_args(payload.get("input"))
    return label, _tool_target(args)


def render_tool_markdown_group(items: list[tuple[str, str]]) -> str:
    """Collapse consecutive identical tools into a Markdown list."""
    if not items:
        return ""
    lines: list[str] = []
    i = 0
    while i < len(items):
        label, target = items[i]
        n = 1
        while i + n < len(items) and items[i + n] == (label, target):
            n += 1
        if target:
            line = f"- **{label}** `{target}`"
        else:
            line = f"- **{label}**"
        if n > 1:
            line += f" ×{n}"
        lines.append(line)
        i += n
    return "\n\n" + "\n".join(lines) + "\n\n"


def sse_data(obj: dict[str, Any] | str) -> str:
    if isinstance(obj, str):
        return f"data: {obj}\n\n"
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


def chunk_delta(
    *,
    completion_id: str,
    model: str,
    created: int,
    content: str | None = None,
    finish_reason: str | None = None,
    usage: dict[str, int] | None = None,
    grok: dict[str, Any] | None = None,
) -> dict[str, Any]:
    delta: dict[str, Any] = {}
    if content is not None:
        delta["content"] = content
    choice: dict[str, Any] = {
        "index": 0,
        "delta": delta,
        "finish_reason": finish_reason,
    }
    body: dict[str, Any] = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [choice],
    }
    if usage is not None:
        body["usage"] = usage
    if grok is not None:
        body["grok"] = grok
    return body


async def stream_openai_sse(
    events: AsyncIterator[dict[str, Any]],
    *,
    model: str,
    include_thoughts: bool = False,
    include_usage: bool = False,
    on_session: Any | None = None,
) -> AsyncIterator[str]:
    """
    Convert Grok streaming-json events into OpenAI SSE lines.
    `on_session` optional callback(session_id: str | None).
    """
    completion_id = new_completion_id()
    created = int(time.time())
    saw_end = False
    session_id: str | None = None

    # role chunk first (OpenAI convention)
    yield sse_data(
        chunk_delta(
            completion_id=completion_id,
            model=model,
            created=created,
            content=None,
        )
        | {"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}
    )

    async for event in events:
        etype = event.get("type")
        if etype == "text":
            data = event.get("data") or ""
            if data:
                yield sse_data(
                    chunk_delta(
                        completion_id=completion_id,
                        model=model,
                        created=created,
                        content=str(data),
                    )
                )
        elif etype == "thought":
            if include_thoughts:
                data = event.get("data") or ""
                if data:
                    # Non-standard: prefix thoughts in content for simple clients
                    yield sse_data(
                        chunk_delta(
                            completion_id=completion_id,
                            model=model,
                            created=created,
                            content=f"\n[thinking] {data}",
                        )
                    )
        elif etype == "end":
            saw_end = True
            session_id = event.get("sessionId") or event.get("session_id")
            if on_session:
                on_session(session_id)
            usage_dict = None
            if include_usage or event.get("usage"):
                pt, ct, tt = map_usage(event.get("usage") if isinstance(event.get("usage"), dict) else None)
                usage_dict = {
                    "prompt_tokens": pt,
                    "completion_tokens": ct,
                    "total_tokens": tt,
                }
            grok_meta = {
                "session_id": session_id,
                "stop_reason": event.get("stopReason") or event.get("stop_reason"),
                "num_turns": event.get("num_turns") or event.get("numTurns"),
                "request_id": event.get("requestId") or event.get("request_id"),
                "raw_usage": event.get("usage"),
            }
            yield sse_data(
                chunk_delta(
                    completion_id=completion_id,
                    model=model,
                    created=created,
                    finish_reason="stop",
                    usage=usage_dict if include_usage else None,
                    grok=grok_meta,
                )
            )
        elif etype == "error":
            msg = event.get("message") or "Grok stream error"
            # Emit a final error-ish chunk then DONE
            yield sse_data(
                {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": f"\n[error] {msg}"},
                            "finish_reason": "stop",
                        }
                    ],
                    "grok": {
                        "session_id": session_id,
                        "error": msg,
                        "code": event.get("code"),
                    },
                }
            )
            yield sse_data("[DONE]")
            return

    if not saw_end:
        yield sse_data(
            chunk_delta(
                completion_id=completion_id,
                model=model,
                created=created,
                finish_reason="stop",
                grok={"session_id": session_id},
            )
        )
    yield sse_data("[DONE]")


async def stream_openai_sse_from_journal(
    events: AsyncIterator[Any],
    *,
    model: str,
    response_id: str | None = None,
    include_usage: bool = False,
    on_session: Any | None = None,
) -> AsyncIterator[str]:
    """
    Map orchestrator journal events (EventRecord) to OpenAI chat.completion SSE.

    - response.output_text.delta -> content chunks
    - response.tool_call.started -> Markdown list (WorkBuddy has no tool UI here)
    - response.permission.required -> optional nonstandard grok extension (type permission)
    - response.completed / failed / cancelled / incomplete -> finish + [DONE]
    """
    completion_id = new_completion_id()
    created = int(time.time())
    saw_terminal = False
    session_id: str | None = None
    pending_tools: list[tuple[str, str]] = []

    def _flush_tools() -> str:
        text = render_tool_markdown_group(pending_tools)
        pending_tools.clear()
        return text

    # role chunk first (OpenAI convention)
    yield sse_data(
        chunk_delta(
            completion_id=completion_id,
            model=model,
            created=created,
            content=None,
        )
        | {"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}
    )

    async for ev in events:
        etype = getattr(ev, "event_type", None) or (
            ev.get("event_type") if isinstance(ev, dict) else None
        )
        payload = getattr(ev, "payload_json", None)
        if payload is None and isinstance(ev, dict):
            payload = ev.get("payload_json") or ev.get("data") or {}
        if not isinstance(payload, dict):
            payload = {}

        if etype == "response.tool_call.started":
            pending_tools.append(format_tool_markdown(payload))
        elif etype == "response.output_text.delta":
            tools = _flush_tools()
            if tools:
                yield sse_data(
                    chunk_delta(
                        completion_id=completion_id,
                        model=model,
                        created=created,
                        content=tools,
                    )
                )
            chunk = payload.get("delta")
            if chunk:
                yield sse_data(
                    chunk_delta(
                        completion_id=completion_id,
                        model=model,
                        created=created,
                        content=str(chunk),
                    )
                )
        elif etype == "response.permission.required":
            tools = _flush_tools()
            if tools:
                yield sse_data(
                    chunk_delta(
                        completion_id=completion_id,
                        model=model,
                        created=created,
                        content=tools,
                    )
                )
            # Non-standard extension so chat clients can surface approvals mid-stream
            yield sse_data(
                chunk_delta(
                    completion_id=completion_id,
                    model=model,
                    created=created,
                    grok={
                        "type": "permission",
                        "permission_id": payload.get("permission_id"),
                        "category": payload.get("category"),
                        "risk": payload.get("risk"),
                        "title": payload.get("title"),
                        "arguments": payload.get("arguments"),
                        "options": payload.get("options"),
                        "expires_at": payload.get("expires_at"),
                        "response_id": response_id,
                    },
                )
            )
        elif etype == "response.completed":
            saw_terminal = True
            tools = _flush_tools()
            if tools:
                yield sse_data(
                    chunk_delta(
                        completion_id=completion_id,
                        model=model,
                        created=created,
                        content=tools,
                    )
                )
            session_id = payload.get("session_id") or session_id
            if on_session:
                on_session(session_id)
            usage_raw = payload.get("usage")
            usage_dict = None
            if include_usage or usage_raw:
                pt, ct, tt = map_usage(usage_raw if isinstance(usage_raw, dict) else None)
                usage_dict = {
                    "prompt_tokens": pt,
                    "completion_tokens": ct,
                    "total_tokens": tt,
                }
            yield sse_data(
                chunk_delta(
                    completion_id=completion_id,
                    model=model,
                    created=created,
                    finish_reason="stop",
                    usage=usage_dict if include_usage else None,
                    grok={
                        "session_id": session_id,
                        "stop_reason": "EndTurn",
                        "response_id": response_id or payload.get("id"),
                        "raw_usage": usage_raw,
                    },
                )
            )
            yield sse_data("[DONE]")
            return
        elif etype in ("response.failed", "response.cancelled", "response.incomplete"):
            saw_terminal = True
            tools = _flush_tools()
            if tools:
                yield sse_data(
                    chunk_delta(
                        completion_id=completion_id,
                        model=model,
                        created=created,
                        content=tools,
                    )
                )
            session_id = payload.get("session_id") or session_id
            if on_session and session_id:
                on_session(session_id)
            msg = payload.get("message") or payload.get("reason") or etype
            code = payload.get("code") or (
                "timeout" if payload.get("reason") == "timeout" else etype.split(".")[-1]
            )
            is_timeout = etype == "response.incomplete" and (
                payload.get("reason") == "timeout" or code == "timeout"
            )
            finish = "length" if is_timeout else "stop"
            content_prefix = ""
            if etype == "response.failed":
                content_prefix = f"\n[error] {msg}"
            elif is_timeout:
                content_prefix = timeout_client_notice(
                    timeout_sec=payload.get("timeout_sec") or 0,
                    session_id=session_id,
                )
            body = chunk_delta(
                completion_id=completion_id,
                model=model,
                created=created,
                content=content_prefix if content_prefix else None,
                finish_reason=finish,
                grok={
                    "session_id": session_id,
                    "error": msg if etype == "response.failed" else None,
                    "code": code,
                    "response_id": response_id or payload.get("id"),
                    "status": etype.split(".")[-1],
                    "stop_reason": "timeout" if is_timeout else None,
                },
            )
            if content_prefix:
                body["choices"][0]["delta"]["content"] = content_prefix
            yield sse_data(body)
            yield sse_data("[DONE]")
            return

    if not saw_terminal:
        tools = _flush_tools()
        if tools:
            yield sse_data(
                chunk_delta(
                    completion_id=completion_id,
                    model=model,
                    created=created,
                    content=tools,
                )
            )
        yield sse_data(
            chunk_delta(
                completion_id=completion_id,
                model=model,
                created=created,
                finish_reason="stop",
                grok={"session_id": session_id, "response_id": response_id},
            )
        )
    yield sse_data("[DONE]")
