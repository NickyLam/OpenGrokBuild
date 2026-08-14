from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from grok_proxy import __version__
from grok_proxy.protocol.responses_models import response_record_to_object
from grok_proxy.runtime.commands import CreateResponseCommand, GrokExtensions
from grok_proxy.runtime.orchestrator import ResponseOrchestrator

# MCP spec revision this server implements the handshake for.
MCP_PROTOCOL_VERSION = "2024-11-05"


class McpToolRouter:
    """MCP tool surface over ResponseOrchestrator (stdio JSON-RPC lite)."""

    def __init__(self, orchestrator: ResponseOrchestrator, *, default_model: str) -> None:
        self.orchestrator = orchestrator
        self.default_model = default_model

    def list_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "grok_consult",
                "description": (
                    "Read-only consultation with Grok (architecture, analysis). "
                    "Runs in the background by default; poll response_id with grok_status."
                ),
                "inputSchema": {
                    "type": "object",
                    "required": ["prompt", "cwd"],
                    "properties": {
                        "prompt": {"type": "string"},
                        "cwd": {"type": "string"},
                        "max_turns": {"type": "integer"},
                        "model": {"type": "string"},
                        "background": {"type": "boolean", "default": True},
                    },
                },
            },
            {
                "name": "grok_review",
                "description": (
                    "Code review of the workspace (default read-only). Runs in the background "
                    "by default; poll response_id with grok_status."
                ),
                "inputSchema": {
                    "type": "object",
                    "required": ["cwd"],
                    "properties": {
                        "cwd": {"type": "string"},
                        "instructions": {"type": "string"},
                        "scope": {"type": "string"},
                        "model": {"type": "string"},
                        "background": {"type": "boolean", "default": True},
                    },
                },
            },
            {
                "name": "grok_delegate",
                "description": "Delegate a coding task (default worktree isolation).",
                "inputSchema": {
                    "type": "object",
                    "required": ["prompt", "cwd"],
                    "properties": {
                        "prompt": {"type": "string"},
                        "cwd": {"type": "string"},
                        "workspace_mode": {"type": "string"},
                        "permission_policy": {"type": "string"},
                        "background": {"type": "boolean"},
                        "model": {"type": "string"},
                    },
                },
            },
            {
                "name": "grok_status",
                "description": "Get response status.",
                "inputSchema": {
                    "type": "object",
                    "required": ["response_id"],
                    "properties": {"response_id": {"type": "string"}},
                },
            },
            {
                "name": "grok_cancel",
                "description": "Cancel a response.",
                "inputSchema": {
                    "type": "object",
                    "required": ["response_id"],
                    "properties": {"response_id": {"type": "string"}},
                },
            },
            {
                "name": "grok_get_diff",
                "description": "Get workspace diff for a response (worktree).",
                "inputSchema": {
                    "type": "object",
                    "required": ["response_id"],
                    "properties": {"response_id": {"type": "string"}},
                },
            },
            {
                "name": "grok_resume",
                "description": "Continue a prior session with a new prompt.",
                "inputSchema": {
                    "type": "object",
                    "required": ["response_id", "prompt"],
                    "properties": {
                        "response_id": {"type": "string"},
                        "prompt": {"type": "string"},
                    },
                },
            },
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "grok_consult":
            return await self._create(
                prompt=str(arguments["prompt"]),
                cwd=str(arguments["cwd"]),
                model=str(arguments.get("model") or self.default_model),
                workspace_mode="read_only",
                permission_policy="server",
                max_turns=arguments.get("max_turns"),
                background=bool(arguments.get("background", True)),
                always_approve=True,
            )
        if name == "grok_review":
            instructions = str(arguments.get("instructions") or "Review the current changes.")
            scope = str(arguments.get("scope") or "git_diff")
            prompt = f"{instructions}\n\nScope: {scope}"
            return await self._create(
                prompt=prompt,
                cwd=str(arguments["cwd"]),
                model=str(arguments.get("model") or self.default_model),
                workspace_mode="read_only",
                permission_policy="server",
                background=bool(arguments.get("background", True)),
                always_approve=True,
            )
        if name == "grok_delegate":
            background = bool(arguments.get("background", True))
            return await self._create(
                prompt=str(arguments["prompt"]),
                cwd=str(arguments["cwd"]),
                model=str(arguments.get("model") or self.default_model),
                workspace_mode=str(arguments.get("workspace_mode") or "worktree"),
                permission_policy=str(arguments.get("permission_policy") or "ask"),
                background=background,
                always_approve=False,
            )
        if name == "grok_status":
            rec = self.orchestrator.get(str(arguments["response_id"]))
            return response_record_to_object(rec).model_dump()
        if name == "grok_cancel":
            rec = await self.orchestrator.cancel(str(arguments["response_id"]), actor="mcp")
            return response_record_to_object(rec).model_dump()
        if name == "grok_get_diff":
            rid = str(arguments["response_id"])
            rec = self.orchestrator.get(rid)
            live = self.orchestrator._runs.get(rid)  # noqa: SLF001
            diff = ""
            if live and live.workspace:
                diff = self.orchestrator.workspace.collect_diff(live.workspace)
            return {
                "response_id": rid,
                "source_cwd": rec.source_cwd,
                "run_cwd": rec.run_cwd,
                "workspace_mode": rec.workspace_mode,
                "diff": diff,
            }
        if name == "grok_resume":
            prev = self.orchestrator.get(str(arguments["response_id"]))
            return await self._create(
                prompt=str(arguments["prompt"]),
                cwd=str(prev.source_cwd or prev.run_cwd or "."),
                model=prev.model,
                workspace_mode=str(prev.workspace_mode or "read_only"),
                session_id=prev.session_id,
                previous_response_id=prev.id,
                always_approve=True,
            )
        return {"error": f"unknown tool: {name}"}

    async def _create(
        self,
        *,
        prompt: str,
        cwd: str,
        model: str,
        workspace_mode: str,
        permission_policy: str = "server",
        max_turns: int | None = None,
        background: bool = False,
        always_approve: bool = True,
        session_id: str | None = None,
        previous_response_id: str | None = None,
    ) -> dict[str, Any]:
        cmd = CreateResponseCommand(
            model=model,
            input_text=prompt,
            stream=False,
            background=background,
            previous_response_id=previous_response_id,
            x_grok=GrokExtensions(
                cwd=cwd,
                workspace_mode=workspace_mode,  # type: ignore[arg-type]
                permission_policy=permission_policy,  # type: ignore[arg-type]
                max_turns=max_turns,
                always_approve=always_approve,
                session_id=session_id,
            ),
            default_always_approve=always_approve,
            default_cwd=cwd,
        )
        rec = await self.orchestrator.create(cmd)
        obj = response_record_to_object(rec).model_dump()
        if background or rec.status not in ("completed", "failed", "cancelled"):
            return {
                "response_id": rec.id,
                "status": rec.status,
                "message": "Task started; poll with grok_status",
                "response": obj,
            }
        return obj


async def run_mcp_stdio(router: McpToolRouter) -> None:
    """MCP JSON-RPC loop on stdin/stdout.

    Implements the subset real clients (Qoder / Codex / MCP SDK) need:
    initialize handshake, notifications (no reply), ping, tools/list and
    tools/call with the standard content-array result shape. stdin is read in
    a worker thread so background responses keep progressing on the loop.
    """

    def _write(payload: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    pending: set[asyncio.Task[None]] = set()

    async def _dispatch(mid: Any, method: str, params: Any) -> None:
        try:
            try:
                result: Any
                if method == "initialize":
                    result = {
                        "protocolVersion": str(
                            params.get("protocolVersion") or MCP_PROTOCOL_VERSION
                        ),
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "grok-proxy", "version": __version__},
                    }
                elif method == "ping":
                    result = {}
                elif method in ("tools/list", "list_tools"):
                    result = {"tools": router.list_tools()}
                elif method in ("tools/call", "call_tool"):
                    name = params.get("name") or params.get("tool")
                    args = params.get("arguments") or params.get("args") or {}
                    payload = await router.call_tool(str(name), dict(args))
                    is_error = isinstance(payload, dict) and bool(payload.get("error"))
                    # Standard MCP tool result: content array + isError flag
                    result = {
                        "content": [
                            {"type": "text", "text": json.dumps(payload, ensure_ascii=False)}
                        ],
                        "isError": is_error,
                    }
                else:
                    _write(
                        {
                            "jsonrpc": "2.0",
                            "id": mid,
                            "error": {"code": -32601, "message": f"method not found: {method}"},
                        }
                    )
                    return
                _write({"jsonrpc": "2.0", "id": mid, "result": result})
            except Exception as e:  # noqa: BLE001
                _write(
                    {
                        "jsonrpc": "2.0",
                        "id": mid,
                        "error": {"code": -32000, "message": str(e)},
                    }
                )
        finally:
            pending.discard(asyncio.current_task())

    while True:
        line = await asyncio.to_thread(sys.stdin.readline)
        if not line:  # EOF — client closed the pipe
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            return
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict):
            continue
        mid = msg.get("id")
        method = str(msg.get("method") or "")
        if mid is None:
            # Notification (e.g. notifications/initialized) — must not respond
            continue
        params = msg.get("params") or {}
        task = asyncio.create_task(_dispatch(mid, method, params))
        pending.add(task)
