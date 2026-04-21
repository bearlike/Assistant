# Troubleshooting

Quick reference for common failures. For session-level debugging, see the
[debug methodology in CLAUDE.md](https://github.com/bearlike/Assistant/blob/main/CLAUDE.md#debugging-session-errors-trace-methodology).

## LLM connectivity

**Symptom:** Session starts but immediately errors; LLM call fails.

Checks:

1. Verify `llm.api_key` in `configs/app.json` is set and correct.
2. Model name must use `provider/model` syntax: `anthropic/claude-sonnet-4-6`, `openai/gpt-4o`.
3. If using a proxy: set `llm.api_base` and verify `llm.proxy_model_prefix` matches what the proxy expects.
4. Test connectivity directly:

```bash
curl -sk https://api.anthropic.com/v1/messages \
  -H "x-api-key: $KEY" \
  -H "anthropic-version: 2023-06-01" \
  -d '{"model":"claude-haiku-4-5","max_tokens":1,"messages":[{"role":"user","content":"hi"}]}'
```

## Tool not available

**Symptom:** LLM tries to call a tool that doesn't exist; "Tool not available" error in transcript.

Causes:

- LLM referenced a tool that was filtered out by the allowlist or denylist.
- `tool_id` mismatch between what the LLM was told and what's in the registry.
- MCP server not connected (see [MCP server not found](#mcp-server-not-found) below).

Fix: Check `GET /api/tools` (API) or `/mcp` (CLI) to see what tools are actually registered.

## MCP server not found

**Symptom:** "MCP server 'X' not found in config" error.

Causes:

- `configs/mcp.json` path doesn't match the container mount (Docker: paths must be identical between host and container).
- Project `.mcp.json` not merged. CWD not set correctly in the request.
- Key name mismatch: both `mcpServers` and `servers` are accepted. Verify the key in your config file.

Fix: Run `/mcp` in the CLI to see which servers are loaded. Verify the config path with the `--config` flag.

## Shell/file tool errors

**Symptom:** `result: null, success: false` on shell or file tools.

Causes:

- CWD missing in container (volume not mounted, or a different path than the host).
- `root` parameter not injected in the tool call.

Fix: Verify the project directory is mounted at the exact same path as on the host in `docker-compose.override.yml`.

## Docker issues

**Symptom:** Services won't start, or the console can't reach the API.

Checks:

1. Both services use host networking. Verify nothing else is on ports 5125 or 3001.
2. `HOST_UID` and `HOST_GID` must match your actual user: run `id` to find them.
3. Volume paths must match exactly between host and container.
4. Check logs: `docker compose logs -f meeseeks-api`.

## Session stuck / not completing

**Symptom:** Session runs for a very long time or appears to stall.

Notes:

- Meeseeks uses a natural-completion loop. It runs until the LLM emits text with no tool calls. There is no hard step limit enforced at runtime.
- Budget warnings are injected as messages as the context window fills up.
- If a session appears stuck, use mid-session steering:

```bash
POST /api/sessions/{id}/interrupt
```

Or in the CLI: `/terminate`.

Stall detection in the hypervisor fires after repeated identical tool calls and injects a warning message.

## MongoDB connection

**Symptom:** API starts but Web IDE or storage fails; MongoDB connection errors in logs.

Checks:

1. `MEESEEKS_STORAGE_DRIVER=mongodb` is set in the environment.
2. `MEESEEKS_MONGODB_URI` format: `mongodb://user:pass@host:27017/dbname?authSource=admin`.
3. In Docker: MongoDB must be on the same network or accessible via host networking.
4. Default production port: `27017`. (Port `27018` is used for direct dev-environment access.)

## Getting logs

**CLI verbose mode:**

```bash
uv run meeseeks -v    # debug
uv run meeseeks -vv   # trace (very verbose)
```

**Docker API logs:**

```bash
docker compose logs -f meeseeks-api
```

**Langfuse traces** (if enabled):

Session traces appear grouped by `session_id`. Look for `done_reason: "error"` in completion events.
Standard path: `fetch_traces` → `fetch_trace(include_observations=true)` → `fetch_observation` on GENERATION nodes.

## Common config mistakes

| Mistake | Fix |
|---------|-----|
| Model name without provider prefix | Use `anthropic/model`, not just `model` |
| `MASTER_API_TOKEN` left as default | Change before exposing to the network |
| `VITE_API_KEY` doesn't match `MASTER_API_TOKEN` | They must be identical |
| Empty `api_base` with proxy model names | Set `llm.api_base` to your proxy URL |
| Mounted project at different path than host | Container path must equal host path |
