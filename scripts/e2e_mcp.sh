#!/usr/bin/env bash
# MCP stdio smoke for grok-proxy --mcp-stdio
# Usage:
#   ./scripts/e2e_mcp.sh
#   E2E_LIVE_PROMPT=1 ./scripts/e2e_mcp.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

pass=0
fail=0
ok() { echo "  PASS  $1"; pass=$((pass + 1)); }
bad() { echo "  FAIL  $1 — $2"; fail=$((fail + 1)); }

echo "== e2e_mcp =="

run_rpc() {
  local line="$1"
  # one-shot JSON-RPC line
  printf '%s\n' "$line" | uv run grok-proxy --mcp-stdio --database-path "${TMPDIR:-/tmp}/grok-proxy-e2e-mcp.db" 2>/dev/null \
    | head -n 1
}

out=$(run_rpc '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' || true)
if echo "$out" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert "result" in d or "tools" in d.get("result",d); t=d.get("result",d).get("tools",[]); assert any(x.get("name")=="grok_consult" for x in t)' 2>/dev/null; then
  ok "tools/list includes grok_consult"
else
  bad "tools/list" "unexpected: ${out:0:200}"
fi

out=$(run_rpc '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' || true)
if echo "$out" | python3 -c 'import json,sys; d=json.load(sys.stdin); names={t["name"] for t in d["result"]["tools"]}; assert {"grok_status","grok_cancel","grok_delegate"} <= names' 2>/dev/null; then
  ok "core tools present"
else
  bad "core tools" "${out:0:200}"
fi

if [[ "${E2E_LIVE_PROMPT:-0}" == "1" ]]; then
  payload=$(python3 -c 'import json,os; print(json.dumps({"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"grok_consult","arguments":{"prompt":"Reply with exactly the word pong","cwd":os.environ.get("E2E_CWD") or os.getcwd(),"background":False}}}))')
  out=$(printf '%s\n' "$payload" | uv run grok-proxy --mcp-stdio --database-path "${TMPDIR:-/tmp}/grok-proxy-e2e-mcp.db" 2>/dev/null | head -n 1 || true)
  if echo "$out" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert "result" in d; p=json.loads(d["result"]["content"][0]["text"]); assert p.get("status")=="completed"' 2>/dev/null; then
    ok "tools/call grok_consult (live)"
    echo "  body: ${out:0:240}"
  else
    bad "tools/call grok_consult" "${out:0:240}"
  fi
else
  echo "  SKIP  live grok_consult (set E2E_LIVE_PROMPT=1)"
fi

echo "Summary: $pass passed, $fail failed"
[[ "$fail" -eq 0 ]]
