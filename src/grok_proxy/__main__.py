from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

import uvicorn

from grok_proxy.bootstrap import bootstrap_settings
from grok_proxy.config import get_settings


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="grok-proxy",
        description="Grok Build Agent Gateway (HTTP + MCP)",
    )
    p.add_argument(
        "--host",
        default=None,
        help="Bind host (default: GROK_PROXY_HOST or 127.0.0.1)",
    )
    p.add_argument(
        "--port",
        type=int,
        default=None,
        help="Bind port (default: GROK_PROXY_PORT or 8787)",
    )
    p.add_argument(
        "--install-workbuddy",
        action="store_true",
        help="Upsert all discovered Grok models into ~/.workbuddy/models.json (keeps other models)",
    )
    p.add_argument(
        "--no-install-workbuddy",
        action="store_true",
        help="Do not write WorkBuddy models.json even if env enables it",
    )
    p.add_argument(
        "--install-opencode",
        action="store_true",
        help="Upsert provider + all discovered Grok models into ~/.config/opencode/opencode.json",
    )
    p.add_argument(
        "--no-install-opencode",
        action="store_true",
        help="Do not write OpenCode config even if env enables it",
    )
    p.add_argument(
        "--install-pi-agent",
        action="store_true",
        help="Upsert provider + all discovered Grok models into ~/.pi/agent/models.json",
    )
    p.add_argument(
        "--no-install-pi-agent",
        action="store_true",
        help="Do not write Pi Agent models.json even if env enables it",
    )
    p.add_argument(
        "--print-config-only",
        action="store_true",
        help="Generate API key + client config files and exit (do not start server)",
    )
    p.add_argument(
        "--mcp-stdio",
        action="store_true",
        help="Run MCP tool server on stdin/stdout (for Codex/Qoder/etc.)",
    )
    p.add_argument(
        "--database-path",
        default=None,
        help="Override SQLite path for this process",
    )
    return p.parse_args(argv)


def _run_mcp_stdio(settings, database_path: str | None) -> None:
    from grok_proxy.backends.acp import select_backend
    from grok_proxy.concurrency import ConcurrencyGate, PerKeyConcurrencyTracker
    from grok_proxy.grok_runner import GrokRunner
    from grok_proxy.mcp.server import McpToolRouter, run_mcp_stdio
    from grok_proxy.permissions.broker import PermissionBroker
    from grok_proxy.runtime.orchestrator import ResponseOrchestrator
    from grok_proxy.runtime.process_manager import ProcessManager
    from grok_proxy.storage.database import open_database
    from grok_proxy.workspace.manager import WorkspaceManager

    db_path = database_path or settings.database_path or str(
        Path.home() / ".grok-proxy" / "gateway.db"
    )
    db = open_database(db_path)
    gate = ConcurrencyGate(settings.max_concurrent)
    per_key = PerKeyConcurrencyTracker()
    pm = ProcessManager()
    runner = GrokRunner(settings.grok_bin)
    backend = select_backend(
        settings.backend,
        grok_bin=settings.grok_bin,
        runner=runner,
        process_manager=pm,
    )
    orch = ResponseOrchestrator(
        db,
        backend,
        workspace=WorkspaceManager(
            allowlist=settings.cwd_allowlist,
            allow_in_place=settings.allow_in_place,
            default_mode=settings.default_workspace_mode,  # type: ignore[arg-type]
        ),
        permissions=PermissionBroker(db, permission_timeout_sec=settings.permission_timeout_sec),
        process_manager=pm,
        max_concurrent=settings.max_concurrent,
        gate=gate,
        per_key_gate=per_key,
    )
    router = McpToolRouter(orch, default_model=settings.default_model)
    try:
        asyncio.run(run_mcp_stdio(router))
    finally:
        try:
            asyncio.run(orch.shutdown())
        except Exception:  # noqa: BLE001
            pass
        db.close()


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    args = _parse_args(argv)
    settings = get_settings()

    if args.host:
        settings.host = args.host
    if args.port is not None:
        settings.port = args.port
    if args.database_path:
        settings.database_path = args.database_path

    if args.mcp_stdio:
        # Minimal bootstrap for key presence without binding HTTP
        if not settings.api_key.strip():
            result = bootstrap_settings(
                settings,
                install_workbuddy=False,
                install_opencode=False,
                install_pi_agent=False,
                print_banner=False,
            )
            settings = result.settings
        _run_mcp_stdio(settings, args.database_path)
        return

    def _install_flag(yes: bool, no: bool) -> bool | None:
        if no:
            return False
        if yes:
            return True
        return None  # follow env

    install_wb = _install_flag(args.install_workbuddy, args.no_install_workbuddy)
    install_oc = _install_flag(args.install_opencode, args.no_install_opencode)
    install_pi = _install_flag(args.install_pi_agent, args.no_install_pi_agent)

    result = bootstrap_settings(
        settings,
        install_workbuddy=install_wb,
        install_opencode=install_oc,
        install_pi_agent=install_pi,
        print_banner=True,
    )

    if args.print_config_only:
        sys.exit(0)

    uvicorn.run(
        "grok_proxy.main:app",
        host=result.settings.host,
        port=result.settings.port,
        log_level="info",
        factory=False,
    )


if __name__ == "__main__":
    main()

