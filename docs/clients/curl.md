# curl / HTTP integration

## Health

```bash
curl -s http://127.0.0.1:8787/health | jq .
curl -s http://127.0.0.1:8787/ready | jq .
```

## Auth

```bash
export KEY="$(cat ~/.grok-proxy/api_key)"
# or: export KEY=sk-gp-...
```

All `/v1/*` routes (except public health/metrics) need:

```http
Authorization: Bearer $KEY
```

## Models

```bash
curl -s http://127.0.0.1:8787/v1/models \
  -H "Authorization: Bearer $KEY" | jq .

# Force a fresh CLI discovery (the normal endpoint refreshes expired snapshots)
curl -s http://127.0.0.1:8787/v1/models?refresh=true \
  -H "Authorization: Bearer $KEY" | jq .
```

## Connection snapshot (WorkBuddy fragment)

```bash
curl -s http://127.0.0.1:8787/v1/connection \
  -H "Authorization: Bearer $KEY" | jq .
```

## Chat Completions (compat)

```bash
curl -s http://127.0.0.1:8787/v1/chat/completions \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "grok-4.5",
    "messages": [{"role":"user","content":"List top-level files in one short line."}],
    "cwd": "'"$PWD"'",
    "stream": false,
    "x_grok": {"workspace_mode": "read_only"}
  }' | jq '.choices[0].message.content, .grok'
```

Stream:

```bash
curl -sN http://127.0.0.1:8787/v1/chat/completions \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "grok-4.5",
    "messages": [{"role":"user","content":"Say hello in 5 words."}],
    "cwd": "'"$PWD"'",
    "stream": true
  }'
```

## Responses API (preferred for agents)

### Sync

```bash
curl -s http://127.0.0.1:8787/v1/responses \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "input": "Summarize this repo in 3 bullets",
    "x_grok": {"cwd": "'"$PWD"'", "workspace_mode": "read_only"}
  }' | jq '{id, status, text: .x_grok.text}'
```

### Background + poll

```bash
RID=$(curl -s http://127.0.0.1:8787/v1/responses \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "input": "Review recent changes at high level",
    "background": true,
    "x_grok": {"cwd": "'"$PWD"'", "workspace_mode": "read_only"}
  }' | jq -r .id)

echo "response_id=$RID"
curl -s "http://127.0.0.1:8787/v1/responses/$RID" \
  -H "Authorization: Bearer $KEY" | jq '{id, status, text: .x_grok.text}'
```

### Event SSE

```bash
curl -sN "http://127.0.0.1:8787/v1/responses/$RID/events?after=0" \
  -H "Authorization: Bearer $KEY"
# reconnect:
curl -sN "http://127.0.0.1:8787/v1/responses/$RID/events" \
  -H "Authorization: Bearer $KEY" \
  -H "Last-Event-ID: 12"
```

### Cancel

```bash
curl -s -X POST "http://127.0.0.1:8787/v1/responses/$RID/cancel" \
  -H "Authorization: Bearer $KEY" | jq '{id, status}'
```

## Permissions

```bash
# list pending
curl -s "http://127.0.0.1:8787/v1/permissions?status=pending" \
  -H "Authorization: Bearer $KEY" | jq .

# decide (requires permission:approve scope)
curl -s -X POST "http://127.0.0.1:8787/v1/permissions/$PERM_ID/decision" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"decision":"allow_once"}' | jq .
```

## Scoped keys

```bash
# master creates agent key
curl -s http://127.0.0.1:8787/v1/keys \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "agent-smoke",
    "workspace_allowlist": ["'"$PWD"'"],
    "max_concurrent": 1,
    "test": true
  }' | jq .
```

## Metrics

```bash
curl -s http://127.0.0.1:8787/metrics | head
```
