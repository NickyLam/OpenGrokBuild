from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from grok_proxy.errors import ProxyError
from grok_proxy.runtime.process_manager import terminate_process_tree

# optional ProcessManager instance without hard circular import at type-check time

logger = logging.getLogger(__name__)


@dataclass
class GrokRunOptions:
    prompt: str
    model: str
    cwd: str
    stream: bool = False
    session_id: str | None = None
    always_approve: bool = True
    max_turns: int | None = None
    sandbox: str | None = None
    rules: str | None = None
    tools_allow: list[str] | None = None
    tools_deny: list[str] | None = None
    permission_mode: str | None = None
    allow: list[str] | None = None
    deny: list[str] | None = None
    reasoning_effort: str | None = None
    worktree: str | bool | None = None
    timeout_sec: int = 1800
    grok_bin: str = "grok"
    env: dict[str, str] | None = None
    # When set, argv uses --prompt-file instead of embedding prompt after -p
    prompt_file: str | None = None
    # Optional id for ProcessManager tracking
    track_id: str | None = None


@dataclass
class GrokResult:
    text: str = ""
    session_id: str | None = None
    stop_reason: str | None = None
    request_id: str | None = None
    num_turns: int | None = None
    usage: dict[str, Any] | None = None
    model_usage: dict[str, Any] | None = None
    exit_code: int = 0
    raw: dict[str, Any] = field(default_factory=dict)
    stderr_tail: str = ""


def build_grok_argv(opts: GrokRunOptions) -> list[str]:
    fmt = "streaming-json" if opts.stream else "json"
    cmd: list[str] = [opts.grok_bin]
    if opts.prompt_file:
        cmd.extend(["--prompt-file", opts.prompt_file])
    else:
        cmd.extend(["-p", opts.prompt])
    cmd.extend(
        [
            "-m",
            opts.model,
            "--cwd",
            opts.cwd,
            "--output-format",
            fmt,
            "--no-auto-update",
        ]
    )
    if opts.always_approve:
        cmd.append("--always-approve")
    if opts.session_id:
        cmd.extend(["--resume", opts.session_id])
    if opts.max_turns is not None:
        cmd.extend(["--max-turns", str(opts.max_turns)])
    if opts.sandbox:
        cmd.extend(["--sandbox", opts.sandbox])
    if opts.rules:
        cmd.extend(["--rules", opts.rules])
    if opts.tools_allow:
        cmd.extend(["--tools", ",".join(opts.tools_allow)])
    if opts.tools_deny:
        cmd.extend(["--disallowed-tools", ",".join(opts.tools_deny)])
    if opts.permission_mode:
        cmd.extend(["--permission-mode", opts.permission_mode])
    if opts.allow:
        for rule in opts.allow:
            cmd.extend(["--allow", rule])
    if opts.deny:
        for rule in opts.deny:
            cmd.extend(["--deny", rule])
    if opts.reasoning_effort:
        cmd.extend(["--effort", opts.reasoning_effort])
    if opts.worktree is True:
        cmd.append("--worktree")
    elif isinstance(opts.worktree, str) and opts.worktree:
        cmd.extend(["--worktree", opts.worktree])
    return cmd


def _child_env(extra: dict[str, str] | None) -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("GROK_DISABLE_AUTOUPDATER", "1")
    if extra:
        env.update(extra)
    return env


async def _terminate_process(proc: asyncio.subprocess.Process) -> None:
    # Single shared implementation (SIGTERM → SIGKILL, process-group aware)
    await terminate_process_tree(proc)


# Max bytes for one NDJSON line before we drop it (tool results with e.g. web
# search payloads can exceed asyncio's 64 KiB StreamReader default).
MAX_STREAM_LINE_BYTES = 32 * 1024 * 1024


async def iter_ndjson_lines(
    stream: asyncio.StreamReader,
    *,
    max_line_bytes: int = MAX_STREAM_LINE_BYTES,
) -> AsyncIterator[bytes]:
    """Yield newline-delimited lines using read() chunks.

    Unlike StreamReader.readline() this never raises LimitOverrunError on
    oversized lines: a line above max_line_bytes is dropped with a warning and
    iteration continues with the next line.
    """
    buf = bytearray()
    dropping = False
    dropped = 0
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            if buf and not dropping:
                yield bytes(buf)
            return
        buf.extend(chunk)
        while True:
            nl = buf.find(b"\n")
            if nl == -1:
                if len(buf) > max_line_bytes:
                    dropping = True
                    dropped += len(buf)
                    buf.clear()
                break
            line = bytes(buf[:nl])
            del buf[: nl + 1]
            if dropping:
                # Tail of an oversized line — discard and resume normal parsing
                dropped += nl + 1
                logger.warning("dropped oversized stream line (~%d bytes)", dropped)
                dropping = False
                dropped = 0
                continue
            yield line


def parse_json_result(payload: dict[str, Any], *, exit_code: int, stderr_tail: str = "") -> GrokResult:
    # Headless error object: {"type":"error","message":"..."}
    if payload.get("type") == "error":
        msg = payload.get("message") or payload.get("error") or str(payload)
        raise ProxyError(
            f"Grok error: {msg}",
            status_code=502,
            code="grok_error",
            details={"raw": payload, "stderr_tail": stderr_tail},
        )

    usage = payload.get("usage")
    return GrokResult(
        text=str(payload.get("text") or ""),
        session_id=payload.get("sessionId") or payload.get("session_id"),
        stop_reason=payload.get("stopReason") or payload.get("stop_reason"),
        request_id=payload.get("requestId") or payload.get("request_id"),
        num_turns=payload.get("num_turns") or payload.get("numTurns"),
        usage=usage if isinstance(usage, dict) else None,
        model_usage=payload.get("modelUsage") if isinstance(payload.get("modelUsage"), dict) else None,
        exit_code=exit_code,
        raw=payload,
        stderr_tail=stderr_tail,
    )


def map_usage(usage: dict[str, Any] | None) -> tuple[int, int, int]:
    if not usage:
        return 0, 0, 0
    prompt = int(
        usage.get("input_tokens")
        or usage.get("prompt_tokens")
        or usage.get("inputTokens")
        or 0
    )
    # Prefer total if present; cache may inflate total_tokens
    completion = int(
        usage.get("output_tokens")
        or usage.get("completion_tokens")
        or usage.get("outputTokens")
        or 0
    )
    total = int(usage.get("total_tokens") or usage.get("totalTokens") or (prompt + completion))
    return prompt, completion, total


class GrokRunner:
    def __init__(
        self,
        grok_bin: str = "grok",
        *,
        process_manager: Any | None = None,
    ) -> None:
        self.grok_bin = grok_bin
        self.process_manager = process_manager

    def _ensure_bin(self, binary: str) -> None:
        path = Path(binary)
        if path.is_file() and os.access(path, os.X_OK):
            return
        # allow PATH lookup
        from shutil import which

        if which(binary):
            return
        raise ProxyError(
            f"Grok binary not found or not executable: {binary!r}. Set GROK_BIN.",
            status_code=503,
            code="grok_not_found",
        )

    async def run(self, opts: GrokRunOptions) -> GrokResult:
        opts.grok_bin = opts.grok_bin or self.grok_bin
        self._ensure_bin(opts.grok_bin)
        cmd = build_grok_argv(opts)
        logger.debug("spawn grok argv_len=%s cwd=%s stream=%s", len(cmd), opts.cwd, opts.stream)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_child_env(opts.env),
                start_new_session=(os.name != "nt"),
            )
        except FileNotFoundError as e:
            raise ProxyError(
                f"Failed to spawn grok: {e}",
                status_code=503,
                code="grok_not_found",
            ) from e

        # Track like stream() does, so a crash can reclaim this child too
        track_id = getattr(opts, "track_id", None) or f"run_{os.getpid()}_{id(opts)}"
        if self.process_manager is not None:
            try:
                await self.process_manager.register(str(track_id), proc, kind="run")
            except Exception:  # noqa: BLE001
                logger.exception("process register failed")

        try:
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=opts.timeout_sec,
                )
            except TimeoutError:
                await _terminate_process(proc)
                raise ProxyError(
                    f"Grok timed out after {opts.timeout_sec}s",
                    status_code=504,
                    code="grok_timeout",
                ) from None
        finally:
            if self.process_manager is not None:
                try:
                    await self.process_manager.unregister(str(track_id))
                except Exception:  # noqa: BLE001
                    pass

        stderr_tail = (stderr_b or b"").decode("utf-8", errors="replace")[-4000:]
        stdout = (stdout_b or b"").decode("utf-8", errors="replace").strip()
        code = proc.returncode if proc.returncode is not None else 1

        if not stdout:
            raise ProxyError(
                f"Grok produced empty stdout (exit {code}). stderr: {stderr_tail[-500:]}",
                status_code=502,
                code="grok_empty_output",
                details={"exit_code": code, "stderr_tail": stderr_tail},
            )

        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            # Sometimes CLI prints non-json noise; try last JSON line
            payload = None
            for line in reversed(stdout.splitlines()):
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue
            if payload is None:
                raise ProxyError(
                    f"Grok stdout is not valid JSON (exit {code}). "
                    f"First 200 chars: {stdout[:200]!r}",
                    status_code=502,
                    code="grok_bad_json",
                    details={"exit_code": code, "stderr_tail": stderr_tail},
                ) from None

        if not isinstance(payload, dict):
            raise ProxyError("Grok JSON root must be an object", status_code=502, code="grok_bad_json")

        if code != 0 and payload.get("type") == "error":
            return parse_json_result(payload, exit_code=code, stderr_tail=stderr_tail)

        result = parse_json_result(payload, exit_code=code, stderr_tail=stderr_tail)
        if code != 0 and not result.text:
            raise ProxyError(
                f"Grok exited with code {code}. stderr: {stderr_tail[-500:]}",
                status_code=502,
                code="grok_nonzero_exit",
                details={"exit_code": code, "raw": payload, "stderr_tail": stderr_tail},
            )
        result.exit_code = code
        return result

    async def stream(self, opts: GrokRunOptions) -> AsyncIterator[dict[str, Any]]:
        """Yield parsed streaming-json event dicts; final yield may be synthetic error."""
        opts = GrokRunOptions(**{**opts.__dict__, "stream": True})
        opts.grok_bin = opts.grok_bin or self.grok_bin
        self._ensure_bin(opts.grok_bin)
        cmd = build_grok_argv(opts)

        track_id = getattr(opts, "track_id", None) or f"run_{os.getpid()}_{id(opts)}"
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_child_env(opts.env),
                start_new_session=(os.name != "nt"),
            )
        except FileNotFoundError as e:
            raise ProxyError(
                f"Failed to spawn grok: {e}",
                status_code=503,
                code="grok_not_found",
            ) from e

        if self.process_manager is not None:
            try:
                await self.process_manager.register(str(track_id), proc, kind="stream")
            except Exception:  # noqa: BLE001
                logger.exception("process register failed")

        assert proc.stdout is not None
        stderr_chunks: list[bytes] = []

        async def _drain_stderr() -> None:
            assert proc.stderr is not None
            while True:
                chunk = await proc.stderr.read(4096)
                if not chunk:
                    break
                stderr_chunks.append(chunk)

        stderr_task = asyncio.create_task(_drain_stderr())
        timed_out = False

        try:
            async with asyncio.timeout(opts.timeout_sec):
                async for line_b in iter_ndjson_lines(proc.stdout):
                    line = line_b.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        logger.warning("skip non-json stream line: %s", line[:120])
                        continue
                    if isinstance(event, dict):
                        yield event
        except TimeoutError:
            timed_out = True
            await _terminate_process(proc)
            yield {
                "type": "error",
                "message": f"Grok timed out after {opts.timeout_sec}s",
                "code": "grok_timeout",
            }
        finally:
            if proc.returncode is None and not timed_out:
                # Client cancelled / generator closed
                await _terminate_process(proc)
            else:
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2)
                except TimeoutError:
                    await _terminate_process(proc)
            if self.process_manager is not None:
                try:
                    await self.process_manager.unregister(str(track_id))
                except Exception:  # noqa: BLE001
                    pass
            await asyncio.gather(stderr_task, return_exceptions=True)

        if timed_out:
            return

        code = proc.returncode if proc.returncode is not None else 1
        if code != 0:
            stderr_tail = b"".join(stderr_chunks).decode("utf-8", errors="replace")[-500:]
            # Only emit if stream didn't already end with error/end
            yield {
                "type": "error",
                "message": f"Grok exited with code {code}. {stderr_tail}",
                "code": "grok_nonzero_exit",
                "exit_code": code,
            }
