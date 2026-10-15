# Codex / agent MCP integration

The proxy exposes tools over **MCP-style JSON-RPC on stdio**:

```bash
uv run grok-proxy --mcp-stdio
```

This speaks **standard MCP JSON-RPC on stdio**: `initialize` handshake,
`notifications/*`, `ping`, `tools/list`, and `tools/call` (content-array results).
Works with official MCP SDK clients (Codex, Qoder, CodeBuddy, Claude-style hosts).

## Tools

| Tool | Default policy | Purpose |
|------|----------------|---------|
| `grok_consult` | read_only + background | Architecture / analysis; returns a `response_id` immediately by default |
| `grok_review` | read_only + background | Code review; returns a `response_id` immediately by default |
| `grok_delegate` | worktree + ask | Full coding task |
| `grok_status` | — | Poll response |
| `grok_models` | — | Show the model snapshot; `refresh: true` forces discovery |
| `grok_cancel` | — | Cancel |
| `grok_get_diff` | — | Worktree diff |
| `grok_resume` | — | Continue session |

## Codex config sketch

Exact Codex config keys evolve; use the pattern below and adapt to your Codex version.

**Option A — spawn as MCP server process**

```toml
# Example shape for ~/.codex/config.toml or project config
[mcp_servers.grok_proxy]
command = "uv"
args = ["run", "--directory", "/absolute/path/to/OpenGrokBuild", "grok-proxy", "--mcp-stdio"]
# env if needed:
# env = { GROK_BIN = "/Users/you/.grok/bin/grok", GROK_PROXY_BACKEND = "acp", GROK_PROXY_DEFAULT_MODEL = "auto" }
```

`GROK_PROXY_DEFAULT_MODEL=auto`（或省略该变量）会在 MCP 启动时优先执行
`grok models`，再以有效缓存作为降级；固定写入某个真实模型 ID 则保留固定模式。
模型清单会在进程启动时读取，并在默认 30 分钟 TTL 到期后按需刷新；修改
配置后仍需重启 Codex 的 MCP 进程。

**Option B — shell wrapper**

```bash
#!/usr/bin/env bash
# scripts/mcp-grok-proxy.sh
set -euo pipefail
cd /absolute/path/to/OpenGrokBuild
exec uv run grok-proxy --mcp-stdio
```

Then point Codex at that script as the MCP command.

## Manual protocol smoke (no Codex UI)

```bash
# list tools
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' \
  | uv run grok-proxy --mcp-stdio

# one-shot consult (uses real Grok if authenticated — may cost tokens)
printf '%s\n' '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"grok_consult","arguments":{"prompt":"Reply with pong only","cwd":"'"$PWD"'","background":false}}}' \
  | uv run grok-proxy --mcp-stdio
```

`grok_consult` and `grok_review` default to `background: true`, so a tool call
returns a `response_id` and the task can be followed with `grok_status`. Pass
`"background": false` when a single request must wait for the final response.
The stdio server continues handling other JSON-RPC requests while any tool call
is running.

To inspect or refresh the model list from Codex, call `grok_models` with an empty
arguments object or with `{"refresh": true}`. The result includes the effective
default model, source (`cli`, `cache`, `config`, or `fallback`), timestamps, and
any last refresh error. Normal consult/review/delegate requests refresh an
expired snapshot automatically; an explicit model on a request remains fixed
for that response.

Or use the script:

```bash
./scripts/e2e_mcp.sh
E2E_LIVE_PROMPT=1 ./scripts/e2e_mcp.sh   # optional live consult
```

## Recommended scopes for Codex

Create a scoped key for agent use (HTTP still uses Bearer; MCP currently shares the process identity of the gateway):

```bash
export KEY="$(cat ~/.grok-proxy/api_key)"
curl -s http://127.0.0.1:8787/v1/keys \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "codex",
    "workspace_allowlist": ["'"$HOME"'/projects"],
    "max_concurrent": 1,
    "max_runtime_sec": 1800,
    "test": true
  }'
```

Do **not** grant `permission:approve` to the agent key. Approve high-risk actions with a separate human token via HTTP:

```bash
curl -s "http://127.0.0.1:8787/v1/permissions?status=pending" \
  -H "Authorization: Bearer $HUMAN_KEY"
```

## Qoder / CodeBuddy

Same MCP stdio command:

```text
command: uv
args: run --directory <repo> grok-proxy --mcp-stdio
```

| Client | Notes |
|--------|--------|
| Qoder | Register as external MCP server; prefer `grok_review` / `grok_consult` first |
| CodeBuddy | Same; long tasks use `grok_delegate` then `grok_status` |

## Troubleshooting

| Issue | Check |
|-------|--------|
| Process exits immediately | Run `uv run grok-proxy --mcp-stdio` and send `tools/list` line |
| Empty tool list | Ensure method name is `tools/list` or `list_tools` |
| Grok not found | `which grok` / `GROK_BIN` |
| Hang on consult / review / delegate | Long task or permission wait — use the returned `response_id` with `grok_status` or `grok_cancel` |
| Model list is old | Call `grok_models` with `refresh=true`, or `GET /v1/models?refresh=true` |
| Protocol mismatch | Official MCP SDK clients may need full SDK; use e2e_mcp.sh to confirm lite protocol |

## Relation to HTTP

MCP and HTTP share the same **ResponseOrchestrator** and SQLite journal when using the same database path (`GROK_PROXY_DATABASE_PATH` / default `~/.grok-proxy/gateway.db`).
