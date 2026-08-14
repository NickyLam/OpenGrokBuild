from __future__ import annotations

import asyncio
import io
import json
import queue
import sys

import pytest

from grok_proxy.backends.base import BackendEvent
from grok_proxy.backends.fake import FakeBackend
from grok_proxy.mcp.server import McpToolRouter, run_mcp_stdio
from grok_proxy.permissions.broker import PermissionBroker
from grok_proxy.runtime.orchestrator import ResponseOrchestrator
from grok_proxy.storage.database import open_database
from grok_proxy.workspace.manager import WorkspaceManager


class _QueueStdin:
    def __init__(self, lines: list[str]) -> None:
        self._lines: queue.Queue[str] = queue.Queue()
        for line in lines:
            self._lines.put(line)

    def readline(self) -> str:
        return self._lines.get()


@pytest.mark.asyncio
async def test_mcp_stdio_list_and_status(tmp_path, monkeypatch):
    db = open_database(tmp_path / "m.db")
    fake = FakeBackend(
        script=[
            BackendEvent(type="text", data={"text": "ok"}),
            BackendEvent(type="end", data={"text": "ok", "session_id": "s"}),
        ]
    )
    orch = ResponseOrchestrator(
        db,
        fake,
        workspace=WorkspaceManager(),
        permissions=PermissionBroker(db),
    )
    router = McpToolRouter(orch, default_model="m")

    # list tools
    tools = router.list_tools()
    assert any(t["name"] == "grok_consult" for t in tools)

    # call consult
    result = await router.call_tool(
        "grok_consult",
        {"prompt": "x", "cwd": str(tmp_path)},
    )
    rid = result.get("response_id") or result.get("id")
    assert rid
    for _ in range(100):
        status = await router.call_tool("grok_status", {"response_id": rid})
        if status["status"] == "completed":
            break
        await asyncio.sleep(0.01)
    assert status["status"] == "completed"
    db.close()


@pytest.mark.asyncio
async def test_mcp_stdio_does_not_block_ping_behind_slow_tool(tmp_path, monkeypatch):
    db = open_database(tmp_path / "m.db")
    fake = FakeBackend(
        script=[BackendEvent(type="end", data={"text": "slow", "session_id": "s"})],
        delay_sec=0.05,
    )
    orch = ResponseOrchestrator(
        db,
        fake,
        workspace=WorkspaceManager(),
        permissions=PermissionBroker(db),
    )
    router = McpToolRouter(orch, default_model="m")
    stdin = _QueueStdin(
        [
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "grok_consult",
                        "arguments": {
                            "prompt": "slow",
                            "cwd": str(tmp_path),
                            "background": False,
                        },
                    },
                }
            )
            + "\n",
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}})
            + "\n",
            "",
        ]
    )
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)

    await run_mcp_stdio(router)

    messages = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [message["id"] for message in messages] == [2, 1]
    db.close()
