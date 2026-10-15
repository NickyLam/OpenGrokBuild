from __future__ import annotations

import json
import time
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from grok_proxy.auth import get_auth_context
from grok_proxy.protocol.responses_models import (
    CreateResponseRequest,
    response_record_to_object,
)
from grok_proxy.runtime.commands import CreateResponseCommand, GrokExtensions
from grok_proxy.runtime.orchestrator import ResponseOrchestrator
from grok_proxy.scopes import Scope


def build_responses_router(
    *,
    require_auth: Any,
    get_cfg: Any,
) -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["responses"])

    def _orch(request: Request) -> ResponseOrchestrator:
        return request.app.state.orchestrator

    def _metrics(request: Request):
        return getattr(request.app.state, "metrics", None)

    @router.post("/responses")
    async def create_response(
        body: CreateResponseRequest,
        request: Request,
        _: None = Depends(require_auth),
        cfg: Any = Depends(get_cfg),
    ):
        auth = get_auth_context(request)
        auth.require_scopes(Scope.RESPONSE_CREATE.value)

        orch = _orch(request)
        registry = getattr(request.app.state, "model_registry", None)
        if registry is not None:
            runtime = (await registry.get()).runtime
        else:
            runtime = request.app.state.runtime_models
        from grok_proxy.model_resolve import resolve_request_model

        model = resolve_request_model(body.model, runtime)
        xg_data: dict[str, Any] = {}
        if body.x_grok is not None:
            xg_data = body.x_grok.model_dump(exclude_none=True)
        # top-level aliases; x_grok wins on conflict
        if body.cwd and "cwd" not in xg_data:
            xg_data["cwd"] = body.cwd
        if body.session_id and "session_id" not in xg_data:
            xg_data["session_id"] = body.session_id

        cwd = xg_data.get("cwd") or cfg.default_cwd
        auth.check_workspace(str(cwd))

        # Scoped key cannot expand beyond its workspace_mode / runtime caps
        if auth.workspace_mode and not xg_data.get("workspace_mode"):
            xg_data["workspace_mode"] = auth.workspace_mode
        if auth.max_runtime_sec is not None:
            req_timeout = xg_data.get("timeout_sec") or cfg.default_timeout_sec
            xg_data["timeout_sec"] = min(int(req_timeout), int(auth.max_runtime_sec))
        # Scoped keys without approve cannot force always_approve open-ended writes
        if not auth.is_master and Scope.PERMISSION_APPROVE.value not in auth.scopes:
            if xg_data.get("permission_policy") == "always_approve" and xg_data.get(
                "workspace_mode"
            ) in ("in_place", "worktree"):
                xg_data["permission_policy"] = "ask"

        from grok_proxy.request_context import get_request_id

        cmd = CreateResponseCommand(
            model=model,
            input_text=body.input_text(),
            stream=body.stream,
            background=body.background,
            previous_response_id=body.previous_response_id,
            metadata={**body.metadata, "actor_id": auth.actor_id, "actor_type": auth.actor_type},
            x_grok=GrokExtensions.from_mapping(xg_data),
            default_always_approve=cfg.always_approve,
            default_timeout_sec=cfg.default_timeout_sec,
            default_cwd=cfg.default_cwd,
            actor_id=auth.actor_id,
            actor_type=auth.actor_type,
            actor_max_concurrent=auth.max_concurrent,
            request_id=get_request_id(request),
        )

        m = _metrics(request)
        t0 = time.time()
        if m:
            m.inc("responses_total", status="started")

        if body.stream and not body.background:
            cmd.background = True
            rec = await orch.create(cmd)
            await orch.start(rec.id)

            async def event_source():
                after = 0
                async for ev in orch.stream_events(rec.id, after_sequence=after):
                    after = ev.sequence_number
                    payload = {
                        "id": ev.id,
                        "response_id": ev.response_id,
                        "sequence_number": ev.sequence_number,
                        "type": ev.event_type,
                        "created_at": int(ev.created_at),
                        "data": ev.payload_json,
                    }
                    yield f"id: {ev.sequence_number}\n"
                    yield f"event: {ev.event_type}\n"
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
                if m:
                    m.observe_duration("responses_duration", time.time() - t0)
                    m.inc("responses_total", status="streamed")

            return StreamingResponse(
                event_source(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        rec = await orch.create(cmd)
        if m:
            m.observe_duration("responses_duration", time.time() - t0)
            m.inc("responses_total", status=rec.status)
        return JSONResponse(content=response_record_to_object(rec).model_dump())

    @router.get("/responses/{response_id}")
    async def get_response(
        response_id: str,
        request: Request,
        _: None = Depends(require_auth),
    ):
        get_auth_context(request).require_scopes(Scope.RESPONSE_READ.value)
        rec = _orch(request).get(response_id)
        return JSONResponse(content=response_record_to_object(rec).model_dump())

    @router.post("/responses/{response_id}/cancel")
    async def cancel_response(
        response_id: str,
        request: Request,
        _: None = Depends(require_auth),
    ):
        auth = get_auth_context(request)
        auth.require_scopes(Scope.RESPONSE_CANCEL.value)
        rec = await _orch(request).cancel(response_id, actor=auth.actor_id)
        m = _metrics(request)
        if m:
            m.inc("responses_total", status="cancelled")
        return JSONResponse(content=response_record_to_object(rec).model_dump())

    @router.get("/responses/{response_id}/events")
    async def stream_response_events(
        response_id: str,
        request: Request,
        _: None = Depends(require_auth),
        after: int | None = None,
    ):
        get_auth_context(request).require_scopes(Scope.EVENT_READ.value)
        orch = _orch(request)
        orch.get(response_id)
        last = request.headers.get("Last-Event-ID")
        after_seq = after if after is not None else (int(last) if last and last.isdigit() else 0)
        m = _metrics(request)
        if m:
            m.inc("sse_connections_total")
            m.set_gauge(
                "sse_connections_active",
                float(m.gauges.get("sse_connections_active{}", 0) or 0) + 1,
            )

        async def event_source():
            try:
                async for ev in orch.stream_events(response_id, after_sequence=after_seq):
                    payload = {
                        "id": ev.id,
                        "response_id": ev.response_id,
                        "sequence_number": ev.sequence_number,
                        "type": ev.event_type,
                        "created_at": int(ev.created_at),
                        "data": ev.payload_json,
                    }
                    yield f"id: {ev.sequence_number}\n"
                    yield f"event: {ev.event_type}\n"
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            finally:
                if m:
                    cur = float(m.gauges.get("sse_connections_active{}", 0) or 0)
                    m.set_gauge("sse_connections_active", max(0.0, cur - 1))

        return StreamingResponse(
            event_source(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return router
