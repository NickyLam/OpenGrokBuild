from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from grok_proxy import __version__
from grok_proxy.api.keys import build_keys_router
from grok_proxy.api.permissions import build_permissions_router
from grok_proxy.api.responses import build_responses_router
from grok_proxy.api_keys import ApiKeyStore, get_or_create_pepper
from grok_proxy.auth import authenticate_request, get_auth_context
from grok_proxy.backends.acp import select_backend
from grok_proxy.bootstrap import bootstrap_settings, resolve_model_settings
from grok_proxy.concurrency import ConcurrencyGate, PerKeyConcurrencyTracker
from grok_proxy.config import Settings, get_settings
from grok_proxy.credentials import generic_client_config, workbuddy_model_entry
from grok_proxy.errors import ProxyError, http_exception_handler, proxy_error_handler
from grok_proxy.grok_runner import GrokResult, GrokRunner, map_usage
from grok_proxy.mcp.server import McpToolRouter
from grok_proxy.metrics import MetricsRegistry
from grok_proxy.model_resolve import (
    RuntimeModelRegistry,
    RuntimeModels,
    get_model_capabilities,
    resolve_request_model,
)
from grok_proxy.models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessageOut,
    Choice,
    GrokMeta,
    ModelCard,
    ModelList,
    Usage,
)
from grok_proxy.openai_sse import (
    new_completion_id,
    stream_openai_sse_from_journal,
)
from grok_proxy.permissions.broker import PermissionBroker
from grok_proxy.prompt_builder import build_prompt
from grok_proxy.request_context import RequestContextMiddleware
from grok_proxy.runtime.commands import CreateResponseCommand, GrokExtensions
from grok_proxy.runtime.orchestrator import ResponseOrchestrator
from grok_proxy.runtime.process_manager import ProcessManager
from grok_proxy.scopes import Scope
from grok_proxy.session_store import SessionStore
from grok_proxy.storage.database import open_database
from grok_proxy.workspace.manager import WorkspaceManager

logger = logging.getLogger("grok_proxy")


def _default_db_path() -> Path:
    return Path.home() / ".grok-proxy" / "gateway.db"


def create_app(
    settings: Settings | None = None,
    *,
    bootstrap: bool = True,
    install_workbuddy: bool | None = None,
    install_opencode: bool | None = None,
    install_pi_agent: bool | None = None,
    print_banner: bool = False,
    runtime_models: RuntimeModels | None = None,
    model_registry: RuntimeModelRegistry | None = None,
    backend: Any | None = None,
    database_path: str | Path | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        cfg = settings if settings is not None else get_settings()
        configured_default = cfg.default_model
        configured_models = cfg.models
        models_rt = runtime_models
        registry = model_registry
        if bootstrap:
            result = bootstrap_settings(
                cfg,
                install_workbuddy=install_workbuddy,
                install_opencode=install_opencode,
                install_pi_agent=install_pi_agent,
                print_banner=print_banner,
            )
            cfg = result.settings
            models_rt = result.runtime_models
            registry = result.model_registry
        elif not cfg.api_key.strip():
            cfg.require_api_key()
        cfg.validate_bind_safety()
        if models_rt is None:
            models_rt = resolve_model_settings(cfg)
        elif cfg.default_model in ("", "auto", "grok-build", "default"):
            # Callers may provide a resolved snapshot while keeping the
            # settings object in automatic mode.
            cfg.default_model = models_rt.default_model
            cfg.models = ",".join(models_rt.available)
        if registry is None:
            registry = RuntimeModelRegistry(
                initial=models_rt,
                configured_default=configured_default,
                configured_models=configured_models,
                grok_bin=cfg.grok_bin,
                refresh_interval_sec=cfg.model_refresh_interval_sec,
            )

        app.state.settings = cfg
        app.state.runtime_models = models_rt
        app.state.model_registry = registry
        app.state.gate = ConcurrencyGate(cfg.max_concurrent)
        app.state.per_key_gate = PerKeyConcurrencyTracker()
        app.state.process_manager = ProcessManager()
        reclaimed = app.state.process_manager.reclaim_stale_pids()
        if reclaimed:
            logger.warning("reclaimed %s stale child process(es) from previous run", reclaimed)
        app.state.runner = GrokRunner(cfg.grok_bin, process_manager=app.state.process_manager)

        db_path = database_path or cfg.database_path or str(_default_db_path())
        app.state.db = open_database(db_path)
        # Session→cwd map persists in SQLite so strict checks survive restarts
        app.state.sessions = SessionStore(app.state.db)
        app.state.workspace = WorkspaceManager(
            allowlist=cfg.cwd_allowlist,
            allow_in_place=cfg.allow_in_place,
            default_mode=cfg.default_workspace_mode,  # type: ignore[arg-type]
        )
        app.state.permissions = PermissionBroker(
            app.state.db,
            permission_timeout_sec=cfg.permission_timeout_sec,
        )
        # Pepper lives in the database (decoupled from the master key); legacy
        # installs that hashed with api_key[:16] keep their existing scoped keys.
        pepper = get_or_create_pepper(
            app.state.db, legacy_pepper=cfg.api_key[:16] if cfg.api_key else ""
        )
        app.state.key_store = ApiKeyStore(app.state.db, pepper=pepper)
        app.state.metrics = MetricsRegistry()

        selected = backend
        if selected is None:
            selected = select_backend(
                cfg.backend,
                grok_bin=cfg.grok_bin,
                runner=app.state.runner,
                process_manager=app.state.process_manager,
            )
        app.state.backend = selected
        app.state.orchestrator = ResponseOrchestrator(
            app.state.db,
            selected,
            workspace=app.state.workspace,
            permissions=app.state.permissions,
            process_manager=app.state.process_manager,
            max_concurrent=cfg.max_concurrent,
            gate=app.state.gate,
            per_key_gate=app.state.per_key_gate,
        )
        app.state.mcp = McpToolRouter(
            app.state.orchestrator,
            default_model=cfg.default_model,
            available_models=models_rt.available,
            model_registry=registry,
        )

        backend_name = getattr(
            getattr(selected, "capabilities", None), "name", type(selected).__name__
        )
        logger.info(
            "grok-proxy v%s host=%s port=%s max_concurrent=%s model=%s backend=%s db=%s always_approve=%s",
            __version__,
            cfg.host,
            cfg.port,
            cfg.max_concurrent,
            cfg.default_model,
            backend_name,
            db_path,
            cfg.effective_always_approve(backend_name=str(backend_name)),
        )
        if cfg.always_approve and str(backend_name) != "headless":
            logger.warning(
                "GROK_PROXY_ALWAYS_APPROVE=true with backend=%s — tools run without human approval",
                backend_name,
            )
        try:
            yield
        finally:
            try:
                await app.state.orchestrator.shutdown()
            except Exception:  # noqa: BLE001
                logger.exception("orchestrator shutdown failed")
            try:
                app.state.db.close()
            except Exception:  # noqa: BLE001
                pass

    app = FastAPI(
        title="OpenGrokBuild",
        version=__version__,
        lifespan=lifespan,
    )
    app.add_middleware(RequestContextMiddleware)
    # Starlette's ExceptionHandler type does not model per-exception handlers;
    # the callables are correct at runtime (FastAPI dispatches by exception type).
    app.add_exception_handler(ProxyError, proxy_error_handler)  # type: ignore[arg-type]
    from fastapi import HTTPException

    app.add_exception_handler(HTTPException, http_exception_handler)  # type: ignore[arg-type]

    def get_cfg(request: Request) -> Settings:
        return request.app.state.settings

    def require_auth(request: Request, cfg: Settings = Depends(get_cfg)) -> None:
        key_store = getattr(request.app.state, "key_store", None)
        authenticate_request(request, cfg, key_store=key_store)

    def health_payload(request: Request) -> dict[str, Any]:
        gate: ConcurrencyGate = request.app.state.gate
        cfg: Settings = request.app.state.settings
        registry: RuntimeModelRegistry | None = getattr(request.app.state, "model_registry", None)
        model_snapshot = registry.snapshot() if registry is not None else None
        runtime_model = model_snapshot.runtime if model_snapshot is not None else None
        host = cfg.host if cfg.host not in ("0.0.0.0", "::") else "127.0.0.1"
        backend = getattr(request.app.state, "backend", None)
        backend_name = getattr(getattr(backend, "capabilities", None), "name", None)
        return {
            "status": "ok",
            "version": __version__,
            "in_flight": gate.in_flight,
            "max_concurrent": gate.limit,
            "default_model": runtime_model.default_model if runtime_model else cfg.default_model,
            "base_url": f"http://{host}:{cfg.port}/v1",
            "model_id": runtime_model.default_model if runtime_model else cfg.default_model,
            "model_source": runtime_model.source if runtime_model else "config",
            "model_stale": model_snapshot.stale if model_snapshot is not None else False,
            "backend": backend_name,
        }

    def ready_payload(request: Request) -> dict[str, Any]:
        cfg: Settings = request.app.state.settings
        checks: dict[str, Any] = {
            "database": False,
            "grok_bin": False,
            "backend": False,
            "workspace": True,
        }
        try:
            request.app.state.db.get_response("__health_probe__")
            checks["database"] = True
        except Exception:  # noqa: BLE001
            checks["database"] = False
        from shutil import which

        checks["grok_bin"] = bool(which(cfg.grok_bin) or Path(cfg.grok_bin).is_file())
        backend = getattr(request.app.state, "backend", None)
        checks["backend"] = backend is not None
        # Refresh gauges
        metrics: MetricsRegistry | None = getattr(request.app.state, "metrics", None)
        gate: ConcurrencyGate = request.app.state.gate
        if metrics is not None:
            metrics.set_gauge("responses_active", float(gate.in_flight))
            try:
                metrics.set_gauge(
                    "permissions_pending",
                    float(request.app.state.db.count_pending_permissions()),
                )
            except Exception:  # noqa: BLE001
                pass
        ok = checks["database"]
        return {"status": "ready" if ok else "not_ready", "checks": checks}

    @app.get("/health")
    async def health(request: Request) -> dict[str, Any]:
        return health_payload(request)

    @app.get("/ready")
    async def ready(request: Request) -> dict[str, Any]:
        return ready_payload(request)

    @app.get("/metrics")
    async def metrics_endpoint(request: Request):
        from fastapi.responses import PlainTextResponse

        metrics: MetricsRegistry = request.app.state.metrics
        gate: ConcurrencyGate = request.app.state.gate
        metrics.set_gauge("responses_active", float(gate.in_flight))
        try:
            metrics.set_gauge(
                "permissions_pending",
                float(request.app.state.db.count_pending_permissions()),
            )
            for status, count in request.app.state.db.count_responses_by_status().items():
                metrics.set_gauge("responses_by_status", float(count), status=status)
        except Exception:  # noqa: BLE001
            pass
        backend = getattr(request.app.state, "backend", None)
        fo = getattr(backend, "failover_count", None)
        if fo is not None:
            metrics.set_gauge("backend_failovers_total", float(fo))
        return PlainTextResponse(metrics.render_prometheus(), media_type="text/plain; version=0.0.4")

    @app.get("/v1/health")
    async def v1_health(request: Request, _: None = Depends(require_auth)) -> dict[str, Any]:
        return health_payload(request)

    @app.get("/v1/connection")
    async def connection_info(
        request: Request,
        _: None = Depends(require_auth),
        cfg: Settings = Depends(get_cfg),
    ) -> dict[str, Any]:
        # Master-only: the payload contains the plaintext master key, so scoped
        # keys must never be able to read it (privilege escalation otherwise).
        auth = get_auth_context(request)
        if not auth.is_master:
            raise ProxyError(
                "/v1/connection is restricted to the master key",
                status_code=403,
                code="master_only",
            )
        host = cfg.host if cfg.host not in ("0.0.0.0", "::") else "127.0.0.1"
        base_url = f"http://{host}:{cfg.port}/v1"
        registry: RuntimeModelRegistry | None = getattr(request.app.state, "model_registry", None)
        runtime = (await registry.get()).runtime if registry is not None else request.app.state.runtime_models
        model_id = runtime.default_model
        caps = get_model_capabilities(model_id)
        client = generic_client_config(
            api_key=cfg.api_key,
            base_url=base_url,
            model_id=model_id,
            caps=caps,
        )
        return {
            "base_url": base_url,
            "api_key": cfg.api_key,
            "model_id": model_id,
            "model": model_id,
            "models": runtime.available or cfg.model_ids(),
            "context_window": caps.context_window,
            "max_output_tokens": caps.max_output_tokens,
            "supports_images": caps.supports_images,
            "supports_reasoning": caps.supports_reasoning,
            "workbuddy": workbuddy_model_entry(
                api_key=cfg.api_key,
                base_url=base_url,
                model_id=model_id,
                caps=caps,
            ),
            "client_config": client,
            "openai_sdk": {
                "base_url": base_url,
                "api_key": cfg.api_key,
                "model": model_id,
            },
            "pi_agent": client["pi_agent"],
            "opencode": client["opencode"],
        }

    @app.get("/v1/models", response_model=ModelList)
    async def list_models(
        request: Request,
        response: Response,
        _: None = Depends(require_auth),
        cfg: Settings = Depends(get_cfg),
        refresh: bool = False,
    ) -> ModelList:
        registry: RuntimeModelRegistry | None = getattr(request.app.state, "model_registry", None)
        if registry is not None:
            snapshot = await registry.get(refresh=refresh)
            runtime = snapshot.runtime
            response.headers["X-Grok-Model-Source"] = runtime.source
            response.headers["X-Grok-Model-Default"] = runtime.default_model
            response.headers["X-Grok-Model-Stale"] = str(snapshot.stale).lower()
            if snapshot.last_success_at is not None:
                response.headers["X-Grok-Model-Last-Success"] = str(snapshot.last_success_at)
        else:
            runtime = request.app.state.runtime_models
        ids = runtime.available or cfg.model_ids()
        return ModelList(
            data=[ModelCard(id=mid, created=0) for mid in ids],
        )

    def _resolve_request_cwd(
        body: ChatCompletionRequest, cfg: Settings, workspace: WorkspaceManager
    ) -> str:
        # Single source of truth for cwd validation (existence + allowlist)
        raw = body.cwd or cfg.default_cwd
        return str(workspace.resolve_and_check(raw))

    def _merge_rules(from_prompt: str | None, from_body: str | None) -> str | None:
        parts = [p for p in (from_prompt, from_body) if p and p.strip()]
        if not parts:
            return None
        return "\n\n".join(parts)

    def _to_openai_response(
        result: GrokResult,
        *,
        model: str,
        response_id: str | None = None,
        finish_reason: str = "stop",
    ) -> ChatCompletionResponse:
        pt, ct, tt = map_usage(result.usage)
        return ChatCompletionResponse(
            id=new_completion_id(),
            created=int(time.time()),
            model=model,
            choices=[
                Choice(
                    message=ChatMessageOut(content=result.text),
                    finish_reason=finish_reason,
                )
            ],
            usage=Usage(
                prompt_tokens=pt,
                completion_tokens=ct,
                total_tokens=tt,
            ),
            grok=GrokMeta(
                session_id=result.session_id,
                stop_reason=result.stop_reason,
                num_turns=result.num_turns,
                request_id=result.request_id,
                raw_usage=result.usage,
                exit_code=result.exit_code,
                response_id=response_id,
            ),
        )

    def _chat_to_command(
        body: ChatCompletionRequest,
        *,
        cfg: Settings,
        prompt: str,
        rules: str | None,
        cwd: str,
        model: str,
        stream: bool,
        backend_name: str | None = None,
    ) -> CreateResponseCommand:
        bn = backend_name or cfg.backend
        default_aa = cfg.effective_always_approve(backend_name=str(bn))
        xg = GrokExtensions(
            cwd=cwd,
            session_id=body.session_id,
            max_turns=body.max_turns,
            sandbox=body.sandbox,
            rules=rules,
            always_approve=body.resolved_always_approve(default_aa),
            tools_allow=body.tools_allow,
            tools_deny=body.tools_deny,
            permission_mode=body.permission_mode,
            allow=body.allow,
            deny=body.deny,
            reasoning_effort=body.reasoning_effort,
            worktree=body.worktree,
            timeout_sec=body.timeout_sec if body.timeout_sec is not None else cfg.default_timeout_sec,
            workspace_mode="read_only",
            include_thoughts=body.include_thoughts,
        )
        # Typed x_grok + model_extra fallback; x_grok wins
        xg_map: dict[str, Any] = {}
        if isinstance(body.x_grok, dict):
            xg_map.update(body.x_grok)
        extra = getattr(body, "model_extra", None) or {}
        if isinstance(extra.get("x_grok"), dict):
            xg_map.update(extra["x_grok"])
        for k, v in xg_map.items():
            if hasattr(xg, k) and v is not None:
                setattr(xg, k, v)
        return CreateResponseCommand(
            model=model,
            input_text=prompt,
            stream=stream,
            background=False,
            x_grok=xg,
            default_always_approve=default_aa,
            default_timeout_sec=cfg.default_timeout_sec,
            default_cwd=cfg.default_cwd,
        )

    def _chat_command_with_auth(
        body: ChatCompletionRequest,
        *,
        cfg: Settings,
        prompt: str,
        rules: str | None,
        cwd: str,
        model: str,
        stream: bool,
        request: Request,
    ) -> CreateResponseCommand:
        from grok_proxy.request_context import get_request_id

        caps = getattr(getattr(request.app.state, "backend", None), "capabilities", None)
        bn = getattr(caps, "name", None) or cfg.backend
        cmd = _chat_to_command(
            body,
            cfg=cfg,
            prompt=prompt,
            rules=rules,
            cwd=cwd,
            model=model,
            stream=stream,
            backend_name=str(bn),
        )
        auth = get_auth_context(request)
        cmd.actor_id = auth.actor_id
        cmd.actor_type = auth.actor_type
        cmd.actor_max_concurrent = auth.max_concurrent
        cmd.request_id = get_request_id(request)
        cmd.metadata = {
            **cmd.metadata,
            "actor_id": auth.actor_id,
            "actor_type": auth.actor_type,
            "request_id": cmd.request_id,
        }
        if auth.max_runtime_sec is not None:
            cur = cmd.x_grok.timeout_sec or cfg.default_timeout_sec
            cmd.x_grok.timeout_sec = min(int(cur), int(auth.max_runtime_sec))
        return cmd

    @app.post("/v1/chat/completions")
    async def chat_completions(
        body: ChatCompletionRequest,
        request: Request,
        _: None = Depends(require_auth),
        cfg: Settings = Depends(get_cfg),
    ):
        auth = get_auth_context(request)
        auth.require_scopes(Scope.RESPONSE_CREATE.value)

        sessions: SessionStore = request.app.state.sessions
        registry: RuntimeModelRegistry | None = getattr(request.app.state, "model_registry", None)
        runtime = (await registry.get()).runtime if registry is not None else request.app.state.runtime_models
        orch: ResponseOrchestrator = request.app.state.orchestrator
        metrics: MetricsRegistry = request.app.state.metrics

        cwd = _resolve_request_cwd(body, cfg, request.app.state.workspace)
        auth.check_workspace(cwd)
        if body.session_id:
            sessions.check_cwd(body.session_id, cwd, strict=cfg.strict_session_cwd)

        prompt, sys_rules = build_prompt(body.messages, session_id=body.session_id)
        rules = _merge_rules(sys_rules, body.rules)
        # Omitted model follows the registry snapshot. Settings.default_model is
        # the id materialized at startup and would ignore a later catalog refresh.
        model = resolve_request_model(body.model, runtime)

        logger.info(
            "chat.completions model=%s requested=%s cwd=%s stream=%s resume=%s prompt_chars=%s actor=%s",
            model,
            body.model,
            cwd,
            body.stream,
            bool(body.session_id),
            len(prompt),
            auth.actor_id,
        )
        metrics.inc("chat_completions_total", stream=str(body.stream).lower())

        # Streaming: CreateResponseCommand (stream+background) → Orchestrator journal → OpenAI SSE
        if body.stream:
            cmd = _chat_command_with_auth(
                body,
                cfg=cfg,
                prompt=prompt,
                rules=rules,
                cwd=cwd,
                model=model,
                stream=True,
                request=request,
            )
            cmd.background = True
            rec = await orch.create(cmd)
            await orch.start(rec.id)

            include_usage = bool(
                body.stream_options and body.stream_options.include_usage
            )

            async def event_source():
                def on_session(sid: str | None) -> None:
                    if sid:
                        sessions.remember(sid, cwd)

                async for line in stream_openai_sse_from_journal(
                    orch.stream_events(rec.id, after_sequence=0),
                    model=model,
                    response_id=rec.id,
                    include_usage=include_usage,
                    on_session=on_session,
                ):
                    yield line
                # Prefer session from terminal record if stream missed it
                try:
                    final = orch.get(rec.id)
                    if final.session_id:
                        sessions.remember(final.session_id, cwd)
                except Exception:  # noqa: BLE001
                    pass

            return StreamingResponse(
                event_source(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        # Non-stream: CreateResponseCommand → Orchestrator → Backend
        cmd = _chat_command_with_auth(
            body,
            cfg=cfg,
            prompt=prompt,
            rules=rules,
            cwd=cwd,
            model=model,
            stream=False,
            request=request,
        )
        rec = await orch.create(cmd)
        if rec.session_id:
            sessions.remember(rec.session_id, cwd)

        if rec.status == "failed":
            raise ProxyError(
                rec.error_message or "response failed",
                status_code=502,
                code=rec.error_code or "response_failed",
            )

        result = GrokResult(
            text=rec.text,
            session_id=rec.session_id,
            stop_reason="EndTurn" if rec.status == "completed" else rec.status,
            usage=rec.usage_json,
            exit_code=0 if rec.status == "completed" else 1,
        )
        finish = "length" if rec.status == "incomplete" else "stop"
        payload = _to_openai_response(
            result, model=model, response_id=rec.id, finish_reason=finish
        ).model_dump()
        return JSONResponse(content=payload)

    app.include_router(build_responses_router(require_auth=require_auth, get_cfg=get_cfg))
    app.include_router(build_permissions_router(require_auth=require_auth))
    app.include_router(build_keys_router(require_auth=require_auth))

    return app


# Default app for `uvicorn grok_proxy.main:app`
app = create_app()
