from __future__ import annotations

import asyncio

import pytest

from grok_proxy.backends.base import BackendEvent
from grok_proxy.backends.fake import FakeBackend
from grok_proxy.permissions.broker import PermissionBroker
from grok_proxy.runtime.commands import CreateResponseCommand, GrokExtensions
from grok_proxy.runtime.orchestrator import ResponseOrchestrator
from grok_proxy.storage.database import open_database
from grok_proxy.workspace.manager import WorkspaceManager


@pytest.mark.asyncio
async def test_orchestrator_fake_complete(tmp_path):
    db = open_database(tmp_path / "o.db")
    fake = FakeBackend(
        script=[
            BackendEvent(type="text", data={"text": "ok"}),
            BackendEvent(
                type="end",
                data={
                    "session_id": "s1",
                    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                    "text": "ok",
                },
            ),
        ]
    )
    orch = ResponseOrchestrator(
        db,
        fake,
        workspace=WorkspaceManager(),
        permissions=PermissionBroker(db),
    )
    rec = await orch.create(
        CreateResponseCommand(
            model="m",
            input_text="hi",
            x_grok=GrokExtensions(cwd=str(tmp_path), workspace_mode="read_only"),
            default_cwd=str(tmp_path),
        )
    )
    assert rec.status == "completed"
    assert rec.text == "ok"
    events = list(db.list_events(rec.id, after_sequence=0))
    types = [e.event_type for e in events]
    assert "response.created" in types
    assert "response.completed" in types
    # replay
    async for ev in orch.stream_events(rec.id, after_sequence=0):
        assert ev.sequence_number >= 1
    db.close()


@pytest.mark.asyncio
async def test_orchestrator_rejects_backend_stream_without_final_event(tmp_path):
    db = open_database(tmp_path / "no-final.db")
    fake = FakeBackend(script=[BackendEvent(type="text", data={"text": "partial"})])
    orch = ResponseOrchestrator(db, fake, workspace=WorkspaceManager())

    rec = await orch.create(
        CreateResponseCommand(
            model="m",
            input_text="hi",
            x_grok=GrokExtensions(cwd=str(tmp_path), workspace_mode="read_only"),
            default_cwd=str(tmp_path),
        )
    )

    assert rec.status == "failed"
    assert rec.error_code == "backend_no_final_event"
    db.close()


@pytest.mark.asyncio
async def test_orchestrator_cancel(tmp_path):
    db = open_database(tmp_path / "c.db")
    fake = FakeBackend(
        script=[
            BackendEvent(type="text", data={"text": "a"}),
            BackendEvent(type="end", data={"text": "a"}),
        ],
        delay_sec=0.2,
    )
    orch = ResponseOrchestrator(db, fake, workspace=WorkspaceManager())
    rec = await orch.create(
        CreateResponseCommand(
            model="m",
            input_text="hi",
            background=True,
            x_grok=GrokExtensions(cwd=str(tmp_path)),
            default_cwd=str(tmp_path),
        )
    )
    cancelled = await orch.cancel(rec.id)
    assert cancelled.status in ("cancelled", "completed")
    # idempotent
    again = await orch.cancel(rec.id)
    assert again.status in ("cancelled", "completed")
    db.close()


@pytest.mark.asyncio
async def test_runs_dict_cleaned_after_terminal(tmp_path):
    """_runs must not leak live entries once responses reach a terminal state."""
    db = open_database(tmp_path / "leak.db")
    fake = FakeBackend(
        script=[
            BackendEvent(type="text", data={"text": "ok"}),
            BackendEvent(type="end", data={"text": "ok"}),
        ]
    )
    orch = ResponseOrchestrator(db, fake, workspace=WorkspaceManager())
    ids = []
    for _ in range(3):
        rec = await orch.create(
            CreateResponseCommand(
                model="m",
                input_text="hi",
                x_grok=GrokExtensions(cwd=str(tmp_path)),
                default_cwd=str(tmp_path),
            )
        )
        assert rec.status == "completed"
        ids.append(rec.id)
    for rid in ids:
        assert rid not in orch._runs
    assert orch._runs == {}
    db.close()


@pytest.mark.asyncio
async def test_permission_wait_pauses_response_timeout(tmp_path):
    """Human approval latency must not count against the response timeout."""
    db = open_database(tmp_path / "pt.db")
    fake = FakeBackend(
        script=[
            BackendEvent(
                type="permission_request",
                data={
                    "id": "tc1",
                    "category": "shell",
                    "risk": "medium",
                    "arguments": {"command": "make build"},
                    "title": "Run make build",
                },
            ),
            BackendEvent(type="text", data={"text": "done"}),
            BackendEvent(type="end", data={"text": "done"}),
        ]
    )
    orch = ResponseOrchestrator(
        db,
        fake,
        workspace=WorkspaceManager(),
        permissions=PermissionBroker(db, permission_timeout_sec=30),
    )
    rec = await orch.create(
        CreateResponseCommand(
            model="m",
            input_text="hi",
            background=True,
            x_grok=GrokExtensions(
                cwd=str(tmp_path),
                # Response timeout much shorter than the approval wait below
                timeout_sec=1,
                permission_policy="ask",
            ),
            default_cwd=str(tmp_path),
        )
    )
    await orch.start(rec.id)

    # Wait until the pending permission exists
    perm_id = None
    for _ in range(100):
        pending = db.list_permissions(status="pending", response_id=rec.id)
        if pending:
            perm_id = pending[0].id
            break
        await asyncio.sleep(0.02)
    assert perm_id, "permission request never surfaced"

    # Approve only after the 1s response timeout would have fired
    await asyncio.sleep(1.2)
    await orch.decide_permission(perm_id, decision="allow_once", actor_id="tester")

    await orch._wait_terminal(rec.id, timeout=10)
    final = orch.get(rec.id)
    assert final.status == "completed", (final.status, final.error_code)
    assert final.text == "done"
    db.close()
