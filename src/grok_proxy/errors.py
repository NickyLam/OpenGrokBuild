from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse


def timeout_client_notice(*, timeout_sec: int | float, session_id: str | None = None) -> str:
    """Visible hint so chat clients do not treat a proxy timeout as a clean end."""
    sid = f" Session id: {session_id}." if session_id else ""
    return (
        f"\n\n[proxy] Timed out after {int(timeout_sec)}s. "
        f"Send continue to resume this run.{sid}"
    )


class ProxyError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int = 400,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code
        self.details = details or {}


def openai_error_body(message: str, *, err_type: str = "invalid_request_error", code: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "error": {
            "message": message,
            "type": err_type,
            "param": None,
            "code": code,
        }
    }
    return body


async def proxy_error_handler(_request: Request, exc: ProxyError) -> JSONResponse:
    err_type = "invalid_request_error"
    if exc.status_code == 401:
        err_type = "authentication_error"
    elif exc.status_code == 403:
        err_type = "permission_error"
    elif exc.status_code == 429:
        err_type = "rate_limit_error"
    elif exc.status_code >= 500:
        err_type = "server_error"
    return JSONResponse(
        status_code=exc.status_code,
        content=openai_error_body(exc.message, err_type=err_type, code=exc.code),
    )


async def http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail
    if isinstance(detail, dict) and "error" in detail:
        return JSONResponse(status_code=exc.status_code, content=detail)
    message = detail if isinstance(detail, str) else str(detail)
    err_type = "invalid_request_error"
    if exc.status_code == 401:
        err_type = "authentication_error"
    elif exc.status_code >= 500:
        err_type = "server_error"
    return JSONResponse(
        status_code=exc.status_code,
        content=openai_error_body(message, err_type=err_type),
    )
