from __future__ import annotations

import asyncio

import pytest

from grok_proxy.backends.base import BackendEvent
from grok_proxy.backends.fake import FakeBackend
from grok_proxy.mcp.server import McpToolRouter
from grok_proxy.model_resolve import RuntimeModelRegistry, RuntimeModels
from grok_proxy.permissions.broker import PermissionBroker
from grok_proxy.runtime.orchestrator import ResponseOrchestrator
from grok_proxy.storage.database import open_database
from grok_proxy.workspace.manager import WorkspaceManager


@pytest.mark.asyncio
async def test_mcp_consult_and_status(tmp_path):
    db = open_database(tmp_path / "m.db")
    fake = FakeBackend(
        script=[
            BackendEvent(type="text", data={"text": "review ok"}),
            BackendEvent(type="end", data={"text": "review ok", "session_id": "s"}),
        ]
    )
    orch = ResponseOrchestrator(
        db,
        fake,
        workspace=WorkspaceManager(),
        permissions=PermissionBroker(db),
    )
    router = McpToolRouter(orch, default_model="m")
    tools = {t["name"] for t in router.list_tools()}
    assert "grok_consult" in tools
    assert "grok_delegate" in tools
    result = await router.call_tool(
        "grok_consult",
        {"prompt": "analyze", "cwd": str(tmp_path)},
    )
    assert result["status"] == "queued"
    assert result["response_id"]
    rid = result["response_id"]

    status = result["response"]
    for _ in range(100):
        status = await router.call_tool("grok_status", {"response_id": rid})
        if status["status"] == "completed":
            break
        await asyncio.sleep(0.01)
    assert status["id"] == rid
    assert status["status"] == "completed"
    assert status["x_grok"]["text"] == "review ok"
    db.close()


@pytest.mark.asyncio
async def test_mcp_model_alias_uses_runtime_default(tmp_path):
    db = open_database(tmp_path / "m.db")
    fake = FakeBackend(
        script=[BackendEvent(type="end", data={"text": "ok", "session_id": "s"})]
    )
    orch = ResponseOrchestrator(
        db,
        fake,
        workspace=WorkspaceManager(),
        permissions=PermissionBroker(db),
    )
    router = McpToolRouter(
        orch,
        default_model="grok-4.6",
        available_models=["grok-4.6", "grok-4.5"],
    )

    result = await router.call_tool(
        "grok_consult",
        {"prompt": "x", "cwd": str(tmp_path), "model": "auto", "background": False},
    )

    assert result["status"] == "completed"
    assert fake.started[-1].model == "grok-4.6"
    db.close()


@pytest.mark.asyncio
async def test_mcp_models_tool_returns_refresh_metadata(tmp_path, monkeypatch):
    db = open_database(tmp_path / "m.db")
    fake = FakeBackend()
    orch = ResponseOrchestrator(
        db,
        fake,
        workspace=WorkspaceManager(),
        permissions=PermissionBroker(db),
    )
    monkeypatch.setattr(
        "grok_proxy.model_resolve.resolve_runtime_models",
        lambda **_kwargs: RuntimeModels(
            default_model="grok-4.6",
            available=["grok-4.6", "grok-4.5"],
            source="cli",
        ),
    )
    registry = RuntimeModelRegistry(
        initial=RuntimeModels(
            default_model="grok-4.5",
            available=["grok-4.5"],
            source="cache",
        ),
        configured_default="auto",
    )
    router = McpToolRouter(
        orch,
        default_model="grok-4.5",
        available_models=["grok-4.5"],
        model_registry=registry,
    )

    result = await router.call_tool("grok_models", {"refresh": True})

    assert result["default_model"] == "grok-4.6"
    assert result["models"] == ["grok-4.6", "grok-4.5"]
    assert result["source"] == "cli"
    assert result["last_success_at"] is not None
    assert result["stale"] is False
    db.close()
