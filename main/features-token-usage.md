# Token Usage & Caching

Meeseeks tracks token consumption for every session, splits it between the root agent and any sub-agents it spawns, and surfaces the numbers in the web console, the CLI, and the REST API. Prompt caching slashes the per-turn cost of re-sending system prompts and tool schemas. It auto-enables for capable providers (Anthropic, OpenAI, Bedrock) with no configuration.

---

## The context window bar (console)

The context window bar sits in the console navbar and in each session's detail header. It shows how full the root agent's context window is right now.

```
ctx ████░░░░░░░░ 42k/200k
         ▲ reserved-for-compact
```

The bar has three segments:

| Segment | Color | Meaning |
|---------|-------|---------|
| Used fill | Foreground / Primary / Destructive (escalates) | Tokens in the most recent root prompt |
| Reserved | Accent | Buffer reserved for the auto-compact threshold |
| Available | Background | Remaining usable space |

The fill color escalates as the window fills up:

| Remaining | Fill color |
|-----------|-----------|
| > 20% | Foreground (neutral) |
| 10%–20% | Primary (warning) |
| < 10% | Destructive (critical) |

Click the bar to open a popover with a full breakdown. The popover shows current context pressure, peak pressure, billed totals, cache reads/writes, reasoning tokens, and the running compaction count.

---

## Per-turn token chip

Each turn in the session timeline carries a compact token chip. It shows the subtotals for the most recent root LLM call:

| Field | Description |
|-------|-------------|
| Input tokens | Raw prompt size billed by the provider |
| Output tokens | Response tokens billed by the provider |
| Cache read tokens | Tokens served from the provider's prompt cache (billed at a discount) |
| Cache write tokens | Tokens written to the provider's prompt cache this turn |
| Reasoning tokens | Hidden thinking tokens from extended-thinking / o1-class models |

Cache read and write tokens are zero on models that do not support prompt caching. Reasoning tokens are zero on non-thinking models.

---

## Root vs sub-agent split

Meeseeks tracks usage separately for the root agent and any sub-agents it spawns. This lets you see at a glance whether token pressure is coming from the orchestrating agent or from the workers it delegated to. A session that spawns many sub-agents will typically show low root pressure alongside high combined sub-agent totals.

The console footer and the context-bar popover both show root and sub-agent counts in parallel. The API returns them as separate fields so you can build dashboards that split the two.

The console shows current context pressure; the API response exposes the full breakdown (current, peak, and cumulative billable totals) for dashboards and cost tracking.

---

## Usage API

```
GET /api/sessions/{session_id}/usage
X-Api-Key: <your-token>
```

Returns the full usage breakdown as JSON. All token fields are integers; zero for sessions or events that predate cache tracking.

```json
{
  "root_model": "anthropic/claude-sonnet-4-6",
  "root_max_input_tokens": 200000,
  "root_last_input_tokens": 42150,
  "root_utilization": 0.2108,
  "tokens_until_compact": 117850,
  "compact_threshold": 0.8,

  "root_peak_input_tokens": 55000,
  "sub_peak_input_tokens": 31000,

  "root_input_tokens_billed": 310000,
  "sub_input_tokens_billed": 95000,
  "total_input_tokens_billed": 405000,

  "root_output_tokens": 18200,
  "sub_output_tokens": 9400,
  "total_output_tokens": 27600,

  "root_cache_creation_tokens": 12000,
  "root_cache_read_tokens": 180000,
  "root_reasoning_tokens": 0,
  "sub_cache_creation_tokens": 3000,
  "sub_cache_read_tokens": 40000,
  "sub_reasoning_tokens": 0,
  "total_cache_creation_tokens": 15000,
  "total_cache_read_tokens": 220000,
  "total_reasoning_tokens": 0,

  "root_llm_calls": 8,
  "sub_llm_calls": 6,
  "sub_agent_count": 2,

  "compaction_count": 1,
  "compaction_tokens_saved": 28000
}
```

Key fields:

| Field | Description |
|-------|-------------|
| `root_last_input_tokens` | Most recent root prompt size. Drives the context bar fill. |
| `root_utilization` | `root_last_input_tokens / root_max_input_tokens` |
| `tokens_until_compact` | Tokens remaining before auto-compact fires |
| `compact_threshold` | The configured `token_budget.auto_compact_threshold` fraction |
| `root_peak_input_tokens` | Largest input seen on any root call this session |
| `sub_peak_input_tokens` | Sum of per-sub-agent peak inputs |
| `*_input_tokens_billed` | Cumulative billable input (sum across all calls, includes cached portions) |
| `*_cache_read_tokens` | Tokens served from cache |
| `*_cache_creation_tokens` | Tokens written to cache |
| `*_reasoning_tokens` | Hidden thinking tokens billed as output |
| `compaction_tokens_saved` | Cumulative tokens freed by all compaction runs |

---

## CLI usage display

`/tokens` and `/budget` are aliases. They print the same budget table for the current session:

```
/tokens
```

```
Token Budget
┌──────────────────────────┬──────────┐
│ Metric                   │ Value    │
├──────────────────────────┼──────────┤
│ Summary tokens           │ 1 842    │
│ Event tokens             │ 40 308   │
│ Total tokens             │ 42 150   │
│ Context window           │ 200 000  │
│ Remaining                │ 157 850  │
│ Utilization              │ 21.1%    │
│ Auto-compact threshold   │ 80.0%    │
└──────────────────────────┴──────────┘
```

These values use the provider-reported input token count from the most recent LLM response when available, falling back to a local estimate for sessions that have not yet made a call.

---

## Prompt caching

Meeseeks auto-enables provider prompt caching when the model reports it supports caching. No configuration is required. If you are on a supported model, caching is already on.

### Supported providers

| Provider | Cache mechanism | Discount |
|----------|----------------|----------|
| Anthropic | Content-block `cache_control` markers | Cache reads billed at 0.1× input |
| OpenAI | Automatic prefix caching | Cache reads billed at 0.5× input |
| AWS Bedrock | TTL-based caching | Provider-specific |

The per-provider syntax differences are handled transparently. You interact with one consistent usage API regardless of which provider is behind it.

### Proxy models

When using a proxy (`llm.api_base` is set), the proxy must advertise model capabilities for caching to activate. See the architecture page for details. If the proxy does not report caching support, Meeseeks conservatively leaves caching disabled for that model rather than risk malformed requests.

### Seeing cache savings

Cache savings appear immediately in the per-turn chip as **cache read** tokens. Accumulated session savings are visible in the context window bar popover under **Cache reads** (with a tooltip noting the per-provider billing rate). The `/api/sessions/{id}/usage` endpoint surfaces `total_cache_read_tokens` for programmatic access.

---

## Configuration

| Key | Default | Description |
|-----|---------|-------------|
| `token_budget.auto_compact_threshold` | `0.8` | Context fill fraction (0.0–1.0) at which auto-compact fires. |
| `token_budget.default_context_window` | `128000` | Fallback window size when LiteLLM doesn't know the model. |
| `token_budget.model_context_windows` | `{}` | Per-model overrides (map of model name → token count). Use to cap below the real max or for proxy-only models. |
| `llm.compact_models` | `["default"]` | Priority-ordered model list for compaction. |

See [configuration.md](configuration.md#tokenbudgetconfig) for the full schema.

> [!NOTE] How it works internally
> See [Architecture Overview → Token tracking](core-orchestration.md#token-tracking).
