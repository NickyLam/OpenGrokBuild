from __future__ import annotations

import asyncio

from grok_proxy.grok_runner import GrokRunOptions, build_grok_argv, map_usage, parse_json_result


def test_build_argv_basic():
    opts = GrokRunOptions(
        prompt="hi",
        model="grok-build",
        cwd="/tmp/x",
        stream=False,
        always_approve=True,
        max_turns=5,
        session_id="sess-1",
        grok_bin="grok",
    )
    cmd = build_grok_argv(opts)
    assert cmd[0] == "grok"
    assert "-p" in cmd and "hi" in cmd
    assert "--resume" in cmd and "sess-1" in cmd
    assert "--max-turns" in cmd and "5" in cmd
    assert "--always-approve" in cmd
    assert "--output-format" in cmd and "json" in cmd


def test_build_argv_prompt_file():
    opts = GrokRunOptions(
        prompt="",
        model="m",
        cwd="/tmp",
        prompt_file="/secret/prompt.txt",
    )
    cmd = build_grok_argv(opts)
    assert "--prompt-file" in cmd
    assert "/secret/prompt.txt" in cmd
    assert "hi" not in cmd
    assert "-p" not in cmd


def test_build_argv_stream():
    opts = GrokRunOptions(prompt="x", model="m", cwd="/tmp", stream=True)
    cmd = build_grok_argv(opts)
    assert "streaming-json" in cmd


def test_parse_json_result():
    r = parse_json_result(
        {
            "text": "hello",
            "sessionId": "abc",
            "stopReason": "EndTurn",
            "num_turns": 2,
            "usage": {"input_tokens": 10, "output_tokens": 3, "total_tokens": 13},
        },
        exit_code=0,
    )
    assert r.text == "hello"
    assert r.session_id == "abc"
    assert r.num_turns == 2
    pt, ct, tt = map_usage(r.usage)
    assert (pt, ct, tt) == (10, 3, 13)


def test_map_usage_empty():
    assert map_usage(None) == (0, 0, 0)


# ---- iter_ndjson_lines: regression for LimitOverrunError on huge tool results ----


def _reader_with(data: bytes) -> asyncio.StreamReader:
    # Small internal limit to mirror asyncio's 64 KiB default behaviour;
    # iter_ndjson_lines must be immune to it because it uses read(), not readline().
    reader = asyncio.StreamReader(limit=2**16)
    reader.feed_data(data)
    reader.feed_eof()
    return reader


async def test_iter_ndjson_lines_handles_line_over_64k():
    """A single >64 KiB NDJSON line (e.g. web-search tool result) must survive."""
    import json

    from grok_proxy.grok_runner import iter_ndjson_lines

    big = json.dumps({"type": "tool_result", "result": "X" * 100_000}).encode()
    data = b'{"type": "text", "text": "a"}\n' + big + b'\n{"type": "end"}\n'
    lines = [line async for line in iter_ndjson_lines(_reader_with(data))]
    assert len(lines) == 3
    assert json.loads(lines[1])["result"] == "X" * 100_000
    assert json.loads(lines[2]) == {"type": "end"}


async def test_iter_ndjson_lines_drops_pathological_line_and_continues():
    from grok_proxy.grok_runner import iter_ndjson_lines

    huge = b"Y" * 300_000  # above the max_line_bytes cap below
    data = b"first\n" + huge + b"\nlast\n"
    lines = [
        line
        async for line in iter_ndjson_lines(_reader_with(data), max_line_bytes=100_000)
    ]
    assert lines == [b"first", b"last"]


async def test_iter_ndjson_lines_yields_trailing_line_without_newline():
    from grok_proxy.grok_runner import iter_ndjson_lines

    lines = [line async for line in iter_ndjson_lines(_reader_with(b"a\nb"))]
    assert lines == [b"a", b"b"]


def test_loopback_proxy_is_bypassed_for_grok_catalog_hosts():
    from grok_proxy.cli_env import apply_grok_cli_env

    env = apply_grok_cli_env(
        {
            "HTTPS_PROXY": "http://127.0.0.1:7890",
            "NO_PROXY": "localhost",
        }
    )
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:7890"
    assert "cli-chat-proxy.grok.com" in env["NO_PROXY"].split(",")
    assert ".x.ai" in env["NO_PROXY"].split(",")
    assert env["NO_PROXY"].split(",")[0] == "localhost"
    assert env["no_proxy"] == env["NO_PROXY"]
    assert env["GROK_DISABLE_AUTOUPDATER"] == "1"


def test_remote_proxy_is_left_unchanged():
    from grok_proxy.cli_env import apply_grok_cli_env

    env = apply_grok_cli_env({"HTTPS_PROXY": "http://proxy.example.com:8080"})
    assert "NO_PROXY" not in env
    assert env["HTTPS_PROXY"] == "http://proxy.example.com:8080"


def test_catalog_dns_proxy_is_not_bypassed():
    from grok_proxy.cli_env import apply_grok_cli_env

    proxy = "http://127.0.0.1:12345"
    env = apply_grok_cli_env({
        "HTTPS_PROXY": proxy,
        "GROK_PROXY_CATALOG_DNS_PROXY": proxy,
        "NO_PROXY": "localhost,127.0.0.1",
    })
    assert env["HTTPS_PROXY"] == proxy
    assert env["NO_PROXY"] == "localhost,127.0.0.1"


def test_is_unloaded_catalog_model_error():
    from grok_proxy.grok_runner import is_unloaded_catalog_model_error

    message = (
        "Couldn't set model 'grok-4.7': Invalid params: \"unknown model id\". "
        "Run 'grok models' to see available models."
    )
    assert is_unloaded_catalog_model_error({"type": "error", "message": message})
    assert not is_unloaded_catalog_model_error({"type": "error", "message": "Grok exited with code 1."})
    assert not is_unloaded_catalog_model_error({"type": "text", "text": message})


def _fake_grok(tmp_path, body: str):
    script = tmp_path / "grok"
    script.write_text(body, encoding="utf-8")
    script.chmod(0o755)
    return script


async def test_stream_retries_cached_model_after_unknown_model_id(tmp_path, monkeypatch):
    """The first headless process can reject grok-4.7 while its catalog is still
    the built-in pair. A cache hit must spawn a second process and hide the miss.
    """
    from grok_proxy.grok_runner import GrokRunner

    counter = tmp_path / "count"
    script = _fake_grok(
        tmp_path,
        """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
counter = Path(os.environ["GROK_FAKE_COUNTER"])
n = int(counter.read_text() or "0") + 1 if counter.exists() else 1
counter.write_text(str(n))
if n == 1:
    print(json.dumps({
        "type": "error",
        "message": "Couldn't set model 'grok-4.7': Invalid params: \\"unknown model id\\". Run 'grok models' to see available models.",
    }))
    sys.exit(1)
print(json.dumps({"type": "text", "text": "OK"}))
sys.exit(0)
""",
    )
    monkeypatch.setattr("grok_proxy.grok_runner._cached_model_ids", lambda: {"grok-4.7", "grok-4.7-build-fast"})
    runner = GrokRunner(str(script), catalog_miss_retry_delay=0)
    opts = GrokRunOptions(
        prompt="hi",
        model="grok-4.7",
        cwd="/tmp",
        grok_bin=str(script),
        timeout_sec=10,
        env={"GROK_FAKE_COUNTER": str(counter)},
    )
    events = [event async for event in runner.stream(opts)]
    assert events == [{"type": "text", "text": "OK"}]
    assert counter.read_text() == "2"


async def test_stream_does_not_retry_unknown_model_outside_cache(tmp_path, monkeypatch):
    from grok_proxy.grok_runner import GrokRunner

    counter = tmp_path / "count"
    script = _fake_grok(
        tmp_path,
        """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
counter = Path(os.environ["GROK_FAKE_COUNTER"])
n = int(counter.read_text() or "0") + 1 if counter.exists() else 1
counter.write_text(str(n))
print(json.dumps({
    "type": "error",
    "message": "Couldn't set model 'not-a-model': Invalid params: \\"unknown model id\\". Run 'grok models' to see available models.",
}))
sys.exit(1)
""",
    )
    monkeypatch.setattr("grok_proxy.grok_runner._cached_model_ids", lambda: {"grok-4.7"})
    runner = GrokRunner(str(script), catalog_miss_retry_delay=0)
    opts = GrokRunOptions(
        prompt="hi",
        model="not-a-model",
        cwd="/tmp",
        grok_bin=str(script),
        timeout_sec=10,
        env={"GROK_FAKE_COUNTER": str(counter)},
    )
    events = [event async for event in runner.stream(opts)]
    assert counter.read_text() == "1"
    assert any("unknown model id" in str(event.get("message") or "") for event in events)
