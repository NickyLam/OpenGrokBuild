from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from grok_proxy.openai_sse import stream_openai_sse_from_journal
from grok_proxy.storage.models import EventRecord


def _record(seq: int, event_type: str, payload: dict[str, Any]) -> EventRecord:
    return EventRecord(
        id=f"ev_{seq}",
        response_id="resp_1",
        sequence_number=seq,
        event_type=event_type,
        payload_json=payload,
        created_at=0.0,
    )


async def _journal(records: list[EventRecord]) -> AsyncIterator[EventRecord]:
    for record in records:
        yield record


async def _collect(stream: AsyncIterator[str]) -> list[dict[str, Any] | str]:
    parsed: list[dict[str, Any] | str] = []
    async for line in stream:
        assert line.startswith("data: ")
        assert line.endswith("\n\n")
        body = line[len("data: ") : -2]
        parsed.append(body if body == "[DONE]" else json.loads(body))
    return parsed


async def test_output_text_delta_maps_to_content_chunks():
    records = [
        _record(1, "response.output_text.delta", {"delta": "Hello"}),
        _record(2, "response.output_text.delta", {"delta": " world"}),
        _record(3, "response.completed", {"session_id": "sess_1"}),
    ]
    chunks = await _collect(stream_openai_sse_from_journal(_journal(records), model="grok-code"))

    # role chunk first (OpenAI convention)
    assert chunks[0]["object"] == "chat.completion.chunk"
    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert chunks[1]["choices"][0]["delta"]["content"] == "Hello"
    assert chunks[2]["choices"][0]["delta"]["content"] == " world"
    # completion ids must be stable across the stream
    assert chunks[1]["id"] == chunks[0]["id"]
    assert chunks[1]["model"] == "grok-code"
    assert chunks[-1] == "[DONE]"


async def test_completed_emits_finish_and_calls_on_session():
    seen: list[str | None] = []
    records = [_record(1, "response.completed", {"session_id": "sess_9"})]
    chunks = await _collect(
        stream_openai_sse_from_journal(
            _journal(records),
            model="grok-code",
            response_id="resp_1",
            on_session=seen.append,
        )
    )

    assert seen == ["sess_9"]
    final = chunks[-2]
    assert final["choices"][0]["finish_reason"] == "stop"
    assert final["grok"]["session_id"] == "sess_9"
    assert final["grok"]["response_id"] == "resp_1"
    assert chunks[-1] == "[DONE]"


async def test_include_usage_emits_usage_chunk():
    usage = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
    records = [_record(1, "response.completed", {"session_id": "s", "usage": usage})]
    chunks = await _collect(
        stream_openai_sse_from_journal(_journal(records), model="grok-code", include_usage=True)
    )

    final = chunks[-2]
    assert final["usage"] == {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
    }
    assert final["grok"]["raw_usage"] == usage


async def test_failed_emits_error_content_and_done():
    records = [
        _record(1, "response.output_text.delta", {"delta": "partial"}),
        _record(2, "response.failed", {"message": "backend exploded", "code": "acp_dead"}),
    ]
    chunks = await _collect(stream_openai_sse_from_journal(_journal(records), model="grok-code"))

    final = chunks[-2]
    assert final["choices"][0]["finish_reason"] == "stop"
    assert "[error] backend exploded" in final["choices"][0]["delta"]["content"]
    assert final["grok"]["error"] == "backend exploded"
    assert final["grok"]["code"] == "acp_dead"
    assert final["grok"]["status"] == "failed"
    assert chunks[-1] == "[DONE]"


async def test_permission_required_emits_grok_extension_chunk():
    records = [
        _record(
            1,
            "response.permission.required",
            {
                "permission_id": "perm_1",
                "category": "shell",
                "risk": "high",
                "title": "Run command",
            },
        ),
        _record(2, "response.cancelled", {"reason": "user cancel"}),
    ]
    chunks = await _collect(
        stream_openai_sse_from_journal(_journal(records), model="grok-code", response_id="resp_1")
    )

    perm = chunks[1]
    assert perm["grok"]["type"] == "permission"
    assert perm["grok"]["permission_id"] == "perm_1"
    assert perm["grok"]["category"] == "shell"
    assert perm["grok"]["response_id"] == "resp_1"
    cancelled = chunks[-2]
    assert cancelled["grok"]["status"] == "cancelled"
    assert chunks[-1] == "[DONE]"


async def test_incomplete_timeout_uses_length_and_notice():
    seen: list[str | None] = []
    records = [
        _record(1, "response.output_text.delta", {"delta": "working"}),
        _record(
            2,
            "response.incomplete",
            {
                "id": "resp_1",
                "reason": "timeout",
                "code": "timeout",
                "timeout_sec": 600,
                "session_id": "sess_to",
                "message": "timed out after 600s",
            },
        ),
    ]
    chunks = await _collect(
        stream_openai_sse_from_journal(
            _journal(records),
            model="grok-4.6",
            response_id="resp_1",
            on_session=seen.append,
        )
    )

    assert seen == ["sess_to"]
    final = chunks[-2]
    assert final["choices"][0]["finish_reason"] == "length"
    content = final["choices"][0]["delta"]["content"]
    assert "Timed out after 600s" in content
    assert "sess_to" in content
    assert final["grok"]["session_id"] == "sess_to"
    assert final["grok"]["code"] == "timeout"
    assert final["grok"]["status"] == "incomplete"
    assert chunks[-1] == "[DONE]"


async def test_no_terminal_event_synthesizes_finish_and_done():
    records = [_record(1, "response.output_text.delta", {"delta": "hi"})]
    chunks = await _collect(
        stream_openai_sse_from_journal(_journal(records), model="grok-code", response_id="resp_x")
    )

    final = chunks[-2]
    assert final["choices"][0]["finish_reason"] == "stop"
    assert final["grok"] == {"session_id": None, "response_id": "resp_x"}
    assert chunks[-1] == "[DONE]"
