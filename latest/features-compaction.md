# Compaction

LLM context windows are finite. As a session grows, older turns have to make room for newer work. Otherwise you hit the model's context limit and the run falls over. Truss handles this automatically with **compaction**: it summarises older conversation turns into a compact record and injects that summary into each subsequent prompt. The session carries on without losing the thread of work, just with less raw history and more digested history.

Compaction is transparent. You do not have to intervene, and nothing about how you use the session changes after it runs.

**Quick example.** Compact the current session manually:

```
/compact
```

Or via the REST API:

```
POST /api/sessions/{session_id}/query
{"query": "/compact"}
```

---

## When compaction runs

### Automatic

Auto-compact fires when the most recent root prompt crosses `token_budget.auto_compact_threshold` (default 80% of the model's context window). Truss evaluates this after every LLM call using the actual `input_tokens` reported by the provider, not a character-count estimate, so the threshold is accurate even for models with unusual tokenisation.

The context window bar in the console shows how full the window is right now and marks the compact threshold. When the bar reaches that marker, compaction runs before the next turn.

### Manual

| Interface | Command |
|-----------|---------|
| CLI | `/compact` or `/summarize` |
| Console | Compact button in the session header toolbar |
| API | `POST /api/sessions/{id}/query` with `{"query": "/compact"}` |

---

## Two modes: PARTIAL and FULL

Compaction has two modes that trade off differently between context detail and context freshness.

### PARTIAL (default for auto-compact)

Keeps the most recent events verbatim (configurable via `context.recent_event_limit`, default 8) and summarises everything older. The model retains full detail for the current state and a digested summary of what led up to it.

Best for **ongoing work**. The recent context stays intact, so the model does not lose track of the file it is editing or the error it is chasing.

### FULL

Summarises the entire transcript, including recent events. Produces a clean-slate prompt.

Best for **natural task completion** or when you want to reset context pressure before starting a new sub-task in the same session.

### Forcing a mode

Pass the mode explicitly:

```
/compact full
/compact partial
```

Or via the API:

```
POST /api/sessions/{id}/query
{"query": "/compact full"}
```

---

## Caveman mode

Enable `compaction.caveman_mode` to activate a terser summary prompt. It drops articles, filler phrases, pleasantries, and hedging from the prose while preserving code blocks, file paths, URLs, CLI commands, and error strings verbatim. On prose-heavy sessions it reduces compaction output tokens by roughly 30–60% without losing the load-bearing detail.

```json
"compaction": {
  "caveman_mode": true
}
```

---

## After the summary

Once the summary is produced, Truss scans the summarised events for files that were read or edited and re-reads the most recently touched ones into the compacted context. That way, if the model was mid-way through editing a file when compaction fired, it can pick up with the current contents in view rather than having to re-read it.

Compaction is also resilient to sub-agents. Running and completed sub-agent state lives outside the LLM conversation, so compaction never loses track of a spawned worker. The agent tree, their progress notes, and their results all survive.

---

## Routing compaction to a different model

By default compaction uses the session's own model. You can route it to a cheaper or faster model, such as a small Haiku-class model for summarising. Set `llm.compact_models`:

```json
"llm": {
  "compact_models": ["anthropic/claude-haiku-4-5-20251001", "default"]
}
```

Models are tried in priority order; on failure the next entry is used. `"default"` resolves to the running agent's model.

---

## Seeing compactions in the UI

Each compaction appears as a distinct pill in the web console timeline. The context window bar popover includes a **Compactions** row showing how many have run and the total tokens saved across them, so you can tell at a glance how much room has been reclaimed over the life of the session.

---

## Configuration

| Key | Default | Description |
|-----|---------|-------------|
| `token_budget.auto_compact_threshold` | `0.8` | Fraction of the context window (0.0–1.0) at which auto-compact fires. |
| `token_budget.default_context_window` | `128000` | Fallback context window in tokens when the model is not in LiteLLM's catalogue. |
| `token_budget.model_context_windows` | `{}` | Per-model overrides. Use to cap below the model's real max or for proxy-only models. |
| `context.recent_event_limit` | `8` | Events kept verbatim in PARTIAL mode. Everything older is summarised. |
| `llm.compact_models` | `["default"]` | Priority-ordered model list for compaction. `"default"` = agent's own model. |
| `compaction.caveman_mode` | `false` | Enable terse summarization prompt (~30–60% fewer output tokens). |

See [configuration.md](configuration.md#tokenbudgetconfig) for the full schema.

> [!NOTE] How it works internally
> See [Architecture Overview → Compaction pipeline](core-orchestration.md#compaction).
