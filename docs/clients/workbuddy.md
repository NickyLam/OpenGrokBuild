# WorkBuddy custom model

WorkBuddy can treat the proxy as an OpenAI-compatible chat model.

## One-shot install

```bash
uv run grok-proxy --install-workbuddy
```

This upserts **one Custom entry per discovered Grok model** into `~/.workbuddy/models.json`. The default model is listed first. Unrelated WorkBuddy models are kept. `~/.grok-proxy/workbuddy-model.json` is the default-entry shape for manual copy.

The generated entry enables:

| Field | Typical value (`grok-4.5`) | Meaning |
|-------|---------------------------|---------|
| `supportsImages` | `true` | Allow image attachments in the UI |
| `supportsReasoning` | `true` | Expose reasoning effort controls |
| `maxInputTokens` | `500000` | Context window (from Grok models cache) |
| `reasoning.supportedEfforts` | `high` / `medium` / `low` | Available effort levels |

Re-run `--install-workbuddy` after upgrading the proxy so WorkBuddy picks up these fields.

## Manual UI setup

1. Start the proxy: `uv run grok-proxy`
2. Copy from the startup banner (or `GET /v1/connection`):

| Field | Source |
|-------|--------|
| **Base URL / url** | `http://127.0.0.1:8787/v1` |
| **API Key** | `~/.grok-proxy/api_key` |
| **Model ID** | each real id from `grok models` (e.g. `grok-4.6`, `grok-4.5`) |

3. In WorkBuddy → add **Custom** model:
   - Vendor: Custom
   - URL: Base URL above
   - API Key: proxy key
   - Model id: **must** match a CLI id (`grok-4.6`, `grok-4.5`, …), not the old alias `grok-build`

4. Optional flags in WorkBuddy (if the UI supports custom body):
   - Prefer sending `cwd` / `working_directory` to the project path
   - Stream: supported (orchestrator → OpenAI SSE)

## Verify

```bash
# After install, check fragment
cat ~/.grok-proxy/workbuddy-model.json | jq .

# Live chat
export KEY="$(cat ~/.grok-proxy/api_key)"
curl -s http://127.0.0.1:8787/v1/chat/completions \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "grok-4.5",
    "messages": [{"role":"user","content":"ping"}],
    "cwd": "'"$PWD"'"
  }' | jq -r '.choices[0].message.content'
```

## Common errors

| Error | Cause | Fix |
|-------|-------|-----|
| `unknown model id` | UI still has `grok-build` or stale id | Re-run `--install-workbuddy`; set id from `grok models` |
| Connection refused | Proxy not running | `uv run grok-proxy` |
| 401 | Wrong API key | Reload key from `~/.grok-proxy/api_key` |
| Empty reply | Grok auth missing | `grok login` or `XAI_API_KEY` |

## Notes

- Non-stream chat uses the orchestrator and returns `grok.response_id`.
- Stream is journal-backed. Tool calls are rendered as Markdown lists in `delta.content` (WorkBuddy Custom models have no native tool cards here).
- Do not point WorkBuddy at a publicly bound host without auth review.
