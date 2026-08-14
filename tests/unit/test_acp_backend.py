from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from grok_proxy.backends.acp import AcpBackend, _AcpHandle
from grok_proxy.backends.base import (
    BackendError,
    BackendEvent,
    BackendSession,
    BackendSessionRequest,
)


def _make_handle(**kwargs: Any) -> _AcpHandle:
    request = BackendSessionRequest(model="grok-code", cwd="/tmp")
    return _AcpHandle(request=request, **kwargs)


def _attach_fake_proc(handle: _AcpHandle, lines: list[str]) -> None:
    """Simulate the agent's stdout with an in-memory NDJSON StreamReader."""
    reader = asyncio.StreamReader()
    for line in lines:
        reader.feed_data(line.encode("utf-8") + b"\n")
    reader.feed_eof()
    handle.proc = SimpleNamespace(  # type: ignore[assignment]
        stdout=reader,
        stderr=None,
        stdin=None,
        returncode=None,
        pid=0,
    )


def _drain_queue(handle: _AcpHandle) -> list[BackendEvent | None]:
    items: list[BackendEvent | None] = []
    while not handle.event_queue.empty():
        items.append(handle.event_queue.get_nowait())
    return items


async def test_read_loop_resolves_pending_result():
    backend = AcpBackend()
    handle = _make_handle()
    fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    handle.pending[1] = fut
    _attach_fake_proc(
        handle,
        [json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"sessionId": "s1"}})],
    )

    await backend._read_loop(handle)

    assert fut.done()
    assert fut.result() == {"sessionId": "s1"}
    # responses must not be enqueued as backend events; only the EOF sentinel
    assert _drain_queue(handle) == [None]


async def test_read_loop_rejects_pending_error():
    backend = AcpBackend()
    handle = _make_handle()
    fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    handle.pending[7] = fut
    err = {"code": -32000, "message": "boom"}
    _attach_fake_proc(
        handle,
        [json.dumps({"jsonrpc": "2.0", "id": 7, "error": err})],
    )

    await backend._read_loop(handle)

    assert fut.done()
    with pytest.raises(BackendError) as exc_info:
        fut.result()
    assert exc_info.value.code == "acp_rpc_error"
    assert exc_info.value.details == {"error": err}
    assert "boom" in exc_info.value.message


async def test_read_loop_maps_session_update_text():
    backend = AcpBackend()
    handle = _make_handle()
    notification = {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": "s1",
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "hello"},
            },
        },
    }
    _attach_fake_proc(handle, [json.dumps(notification)])

    await backend._read_loop(handle)

    items = _drain_queue(handle)
    assert items[-1] is None
    events = [it for it in items if it is not None]
    assert len(events) == 1
    assert events[0].type == "text"
    assert events[0].data["text"] == "hello"
    # read loop accumulates text chunks for the final "end" event
    assert handle.text_acc == ["hello"]


async def test_read_loop_skips_non_json_and_non_dict_lines():
    backend = AcpBackend()
    handle = _make_handle()
    _attach_fake_proc(
        handle,
        [
            "not json at all",
            "",
            json.dumps(["a", "list", "line"]),
            json.dumps(
                {
                    "method": "session/update",
                    "params": {
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": "ok"},
                        }
                    },
                }
            ),
        ],
    )

    await backend._read_loop(handle)

    items = _drain_queue(handle)
    events = [it for it in items if it is not None]
    assert [ev.data["text"] for ev in events] == ["ok"]


async def test_read_loop_eof_fails_pending_and_signals_end():
    backend = AcpBackend()
    handle = _make_handle()
    fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    handle.pending[3] = fut
    _attach_fake_proc(handle, [])

    await backend._read_loop(handle)

    assert fut.done()
    with pytest.raises(BackendError) as exc_info:
        fut.result()
    assert exc_info.value.code == "acp_dead"
    assert not handle.pending
    assert _drain_queue(handle) == [None]


@pytest.mark.asyncio
async def test_events_report_acp_dead_when_prompt_eof_precedes_result():
    backend = AcpBackend()
    handle = _make_handle(prompt="hello")
    _attach_fake_proc(handle, [])
    session = BackendSession(
        session_id="s1",
        backend_name="acp",
        cwd="/tmp",
        model="grok-code",
        handle=handle,
    )

    await backend._read_loop(handle)

    event = await anext(backend.events(session))
    assert event.type == "error"
    assert event.data["code"] == "acp_dead"


def test_map_notification_permission_request():
    backend = AcpBackend()
    handle = _make_handle()
    params = {
        "requestId": "perm_1",
        "category": "shell",
        "title": "Run command",
        "params": {"command": "ls"},
        "risk": "high",
        "options": [{"optionId": "allow"}, {"optionId": "deny"}],
    }

    ev = backend._map_notification("session/request_permission", params, handle)

    assert ev is not None
    assert ev.type == "permission_request"
    assert ev.data["id"] == "perm_1"
    assert ev.data["category"] == "shell"
    assert ev.data["title"] == "Run command"
    assert ev.data["arguments"] == {"command": "ls"}
    assert ev.data["risk"] == "high"
    assert ev.data["options"] == [{"optionId": "allow"}, {"optionId": "deny"}]
    assert ev.raw is params


def test_map_notification_permission_request_defaults():
    backend = AcpBackend()
    handle = _make_handle()

    ev = backend._map_notification("request_permission", {"id": "p2"}, handle)

    assert ev is not None
    assert ev.type == "permission_request"
    assert ev.data["id"] == "p2"
    assert ev.data["category"] == "unknown"
    assert ev.data["title"] == "Permission required"
    assert ev.data["arguments"] == {}
    assert ev.data["risk"] == "medium"
    assert ev.data["options"] == []


def test_map_notification_tool_call_via_session_update():
    backend = AcpBackend()
    handle = _make_handle()
    params = {
        "sessionId": "s1",
        "update": {
            "sessionUpdate": "tool_call",
            "toolCallId": "tc_1",
            "title": "Read file",
            "rawInput": {"path": "/tmp/x"},
        },
    }

    ev = backend._map_notification("session/update", params, handle)

    assert ev is not None
    assert ev.type == "tool_call"
    assert ev.data["id"] == "tc_1"
    assert ev.data["name"] == "Read file"
    assert ev.data["arguments"] == {"path": "/tmp/x"}


def test_map_notification_thought_respects_include_thoughts():
    backend = AcpBackend()
    params = {
        "update": {
            "sessionUpdate": "agent_thought_chunk",
            "content": {"type": "text", "text": "pondering"},
        }
    }

    plain = _make_handle()
    assert backend._map_notification("session/update", params, plain) is None

    thinking = _make_handle(include_thoughts=True)
    ev = backend._map_notification("session/update", params, thinking)
    assert ev is not None
    assert ev.type == "text"
    assert ev.data["thought"] is True


def test_map_notification_unknown_method_returns_none():
    backend = AcpBackend()
    handle = _make_handle()
    assert backend._map_notification("some/other_method", {"x": 1}, handle) is None
