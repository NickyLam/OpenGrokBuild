# Changelog

## Unreleased

- Chat Completions SSE now renders Grok tool calls as Markdown lists (read/search/run), so WorkBuddy no longer shows one unformatted blob.
- Client install / timeout: proxy timeouts now finish chat streams with `finish_reason=length` plus a visible continue notice, and persist `session_id` so the next turn can resume.
- Known capabilities for `grok-4.6` (500k context). Client default reasoning is `high`, not `xhigh`.
- Default `GROK_PROXY_DEFAULT_TIMEOUT_SEC` is 1800 (was 600).

## 0.2.0

Scheme B foundation: Agent Gateway with Responses API, backend abstraction, and persistence.

### Added

- Backend protocol (`GrokBackend`) with **HeadlessBackend**, **AcpBackend** (live `grok agent stdio`), **FakeBackend**
- **FailoverBackend** (`GROK_PROXY_BACKEND=auto`): ACP primary → Headless on handshake failure
- ACP protocol aligned to CLI 0.2.x: `initialize` → `authenticate` → `session/new` → `session/prompt`
- `scripts/probe_acp.py` and `docs/ACP.md`
- MCP stdio entry: `grok-proxy --mcp-stdio`
- Headless path uses `--prompt-file` (mode `0600`) to avoid leaking prompts in process args
- Full streaming-json event mapping (text, tool, plan, usage, permission, end/error)
- SQLite WAL store: responses, events, permissions, sessions, tool_calls, locks, api_keys, audit_logs
- Response state machine with illegal-transition guards
- `ResponseOrchestrator`: create / start / get / cancel / stream_events / permission decide
- Process manager for process-group terminate
- HTTP API:
  - `POST /v1/responses`
  - `GET /v1/responses/{id}`
  - `POST /v1/responses/{id}/cancel`
  - `GET /v1/responses/{id}/events` (SSE + `Last-Event-ID` / `after`)
  - `GET /v1/permissions/{id}`
  - `POST /v1/permissions/{id}/decision`
  - **Scoped API keys**: `POST/GET /v1/keys`, revoke/enable/disable (hash-only storage)
  - `GET /ready`, `GET /metrics` (Prometheus text)
- Permission policy engine + broker (deny-first, auto-allow, ask)
- Scope enforcement: task keys cannot self-approve (`permission:approve` separated)
- **Per-key concurrency** (`max_concurrent` on key → `429 key_max_concurrent`)
- **Per-key max runtime** caps `timeout_sec`
- Request middleware: `X-Request-ID` echo + structured access logs (`request_id`, `actor`)
- Workspace manager: allowlist, symlink-safe resolve, read/write locks, worktree, diff
- MCP tool router: `grok_consult`, `grok_review`, `grok_delegate`, `grok_status`, `grok_cancel`, `grok_get_diff`, `grok_resume`
- Chat Completions non-stream path routes through Orchestrator; returns `grok.response_id`
- Chat supports nested **`x_grok`** (wins over top-level fields on conflict)
- Docs: ADR-001, architecture, compat matrix, risks, API contract, **UPGRADE-0.2**, **RELEASE-NOTES-0.2**, **SECURITY**
- CI workflow, Ruff/Pyright config, unit + integration tests

### Changed

- Package version **0.2.0**
- Default backend remains `headless` until ACP is verified (`GROK_PROXY_BACKEND=acp|auto`)

### Compatibility

- Existing WorkBuddy / OpenAI Chat Completions clients continue to work (master key)
- v0.1 stream SSE path unchanged (still uses runner stream)

## 0.1.0

- Initial OpenAI Chat Completions proxy over `grok -p`
