# Sub-agents

Mewbo can spawn child agents to work on independent subtasks in parallel. The root agent acts as an orchestrator: it delegates bounded work to sub-agents, monitors their progress, collects structured results, and synthesises a final answer. Sub-agents inherit the parent's permission policy, tool registry, and session context, so they can pick up a task and run with it without re-authorising every tool call.

For setup and installation, see [Getting Started](getting-started.md).

---

## Spawning a sub-agent

The root agent spawns sub-agents by calling the `spawn_agent` tool. Pass a task description; everything else is optional.

```json
{
  "task": "Run the full test suite and report any failures",
  "model": "anthropic/claude-haiku-4-5",
  "allowed_tools": ["read_file", "aider_shell_tool"],
  "denied_tools": [],
  "acceptance_criteria": "Exit code 0 and no FAILED lines in output"
}
```

| Parameter | Type | Required | Description |
|---|---|---|---|
| `task` | string | Yes | Full description of what the sub-agent should do |
| `model` | string | No | Override the model for this agent; must be in `agent.allowed_models` if that list is set |
| `allowed_tools` | array | No | Tool IDs the sub-agent may use; empty means all tools |
| `denied_tools` | array | No | Tool IDs explicitly blocked for this sub-agent |
| `acceptance_criteria` | string | No | How to verify the task is complete (appended to the task description) |
| `agent_type` | string | No | Name of a registered agent definition (e.g. `feature-dev:code-reviewer`); loads a pre-built system prompt, tool scope, and model |

Agent definitions themselves are `.md` files with YAML frontmatter, registered from `~/.claude/agents/`, `.claude/agents/`, and any installed plugin's `agents/` directory. A frontmatter `requires-capabilities` entry gates the definition to sessions that advertise the matching capability. The agent does not appear in the `agent_type` catalog otherwise (see [Plugins & Marketplace → Capability gating](features-plugins.md#capability-gating)). Plugin-contributed agent bodies can reference `${CLAUDE_PLUGIN_ROOT}`, `${SESSION_ID}`, and plugin-specific environment-variable placeholders; these are substituted at spawn time with a single linear `replace` pass. No template engine is used.

### Blocking vs. non-blocking

**When the root agent spawns, the call is non-blocking.** The call returns immediately with an agent ID, whether or not a concurrency slot is free:

```json
{
  "agent_id": "a1b2c3d4-...",
  "status": "submitted",
  "task": "Run the full test suite...",
  "message": "Agent spawned. Use check_agents to monitor progress and collect results."
}
```

`status` is `"submitted"` regardless of whether a slot was free at spawn time: the agent is registered and visible to `check_agents` either way, and starts running on its own the moment a slot frees up if none was available yet. Reaching `agent.max_concurrent` (see [Configuration](#configuration)) is never a reason a spawn fails, and a parent waiting on a child does not itself consume a slot for that wait; only a running agent does.

A spawn is refused outright instead, `status: "rejected"` with no `agent_id`, only when the outcome would be identical on retry: an unresolvable `project`, an unknown `agent_type`, or a model this deployment cannot serve. The response names the reason so the caller does not retry unchanged.

The agent runs in the background. Use `check_agents` to poll or wait for completion.

**When a sub-agent itself spawns a deeper agent, the call is blocking.** The deeper call waits for the child to reach a terminal state, including any time it spends `submitted` before a slot frees, and returns the result inline, so a mid-level agent reads the outcome the moment it is available. Concurrency never fails a blocking spawn either; only the same permanent-refusal reasons above do.

Sub-agents run until the model returns a text response without any more tool calls. That is natural completion. There is no hard step limit. Safety comes from per-call timeouts, stall detection, and the session-wide step budget.

---

## Spawning multiple sub-agents at once

`spawn_agents` fans a batch of independent sub-agents out from one call, the preferred path over issuing several separate `spawn_agent` calls since it guarantees every entry is admitted together in one turn. Each entry takes the same fields as `spawn_agent`.

```json
{
  "tasks": [
    {"task": "Summarize module A"},
    {"task": "Summarize module B"},
    {"task": "Summarize module C"}
  ]
}
```

Every entry that is not a permanent refusal is admitted in the same call: it gets its own `agent_id` and comes back `status: "submitted"`, whether or not a concurrency slot is free for it yet, and each one starts running on its own as a slot becomes available. A 26-entry batch against the default `agent.max_concurrent` of 20, for example, admits all 26: 20 start running right away, 6 stay `"submitted"` until a slot frees, and none are `"rejected"`. The response reports each entry plus totals:

```json
{
  "kind": "agent_batch",
  "text": "Spawned 26/26 agent(s). Use check_agents to monitor progress and collect results.",
  "agents": [
    {"index": 0, "agent_id": "a1b2c3d4-...", "status": "submitted", "task": "Summarize module A"},
    {"index": 1, "agent_id": "e5f6a7b8-...", "status": "submitted", "task": "Summarize module B"}
  ],
  "agent_ids": ["a1b2c3d4-...", "e5f6a7b8-..."],
  "accepted": 26,
  "spawned": 26,
  "dispatched": 20,
  "deferred": 6,
  "rejected": 0
}
```

`accepted` is how many entries became real agents; `spawned` carries the same
number under its historical name. `dispatched` and `deferred` split that total
into the ones running now and the ones waiting for a slot — a scheduling fact
reported as a count, never as a per-agent `status`, so no client has to learn a
state beyond the six an agent can actually be in.

`status` is `"rejected"` only for a permanent refusal, the same reasons a single `spawn_agent` call can be refused for. A rejected entry's `agent_id` is `null` and its `reason` field names why, without affecting its siblings. `agent_ids` preserves order, so index `i` always names `tasks[i]`. Monitor every entry with `check_agents`; a `submitted` entry not yet running reports the same way as one that already is.

---

## Checking and steering agents (root only)

Two root-only tools let the orchestrator observe and influence running sub-agents.

### check_agents

Returns the full agent tree with status, progress notes, and completed results.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `wait` | boolean | `false` | Block until at least one running agent finishes |
| `timeout` | number | `30` | Maximum seconds to wait when `wait=true` |

**Example response (abbreviated):**

```
Agents: 2 running, 1 completed | Budget: 47/500 steps

- [a1b2c3d4] running: "Run the full test suite..." (12 steps, last: aider_shell_tool
  | progress: step 12: aider_shell_tool -> pytest 5 passed...)
- [e5f6g7h8] completed: "Summarise CHANGELOG for v2.1" (3 steps -> success
  | result(completed): Added 14 changelog entries...)
```

### steer_agent

Sends a message to a running agent or cancels it. Agent IDs may be given in full or as a unique 8-character prefix.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `agent_id` | string | Yes | Full agent ID or unique 8-char prefix |
| `action` | string | Yes | `"message"` to inject natural-language feedback; `"cancel"` to stop the agent |
| `message` | string | Conditionally | Required when `action="message"` |

A steering message is queued and delivered to the agent between its next tool steps. It never interrupts an in-flight tool call.

---

## What a sub-agent returns

When a sub-agent finishes, its result is a structured object:

| Field | Type | Description |
|---|---|---|
| `content` | string | Primary output text |
| `status` | string | `completed`, `failed`, `partial`, `cannot_solve`, or `cancelled` |
| `steps_used` | integer | Number of tool steps executed |
| `summary` | string | Compressed summary (≤ 500 chars) for the parent's context |
| `warnings` | array | Non-fatal issues encountered |
| `artifacts` | array | File paths the agent touched |

The `summary` is designed to be short enough that the root agent can keep many completed children in context without blowing up the window; `content` is available when you need the full output.

---

## Configuration

All keys live under `agent` in [`configs/app.json`](configuration.md#agent).

| Key | Type | Default | Description |
|---|---|---|---|
| `agent.max_depth` | integer | `5` | Maximum nesting depth (minimum `1` = no sub-agents) |
| `agent.max_concurrent` | integer | `20` | Maximum number of agents that may be **running** at the same time |
| `agent.default_sub_model` | string | `""` | Default model for sub-agents; inherits root model when empty |
| `agent.allowed_models` | array | `[]` | Allowlist of models sub-agents may use; empty = unrestricted |
| `agent.llm_call_timeout` | float | `120.0` | Per-call timeout in seconds for a single model invocation |
| `agent.llm_call_retries` | integer | `2` | Retries on the primary model before cascading to `llm.fallback_models` |
| `agent.default_denied_tools` | array | `[]` | Tool IDs denied to all sub-agents globally |

`agent.max_concurrent` bounds how many agents may run at once, not how many may be spawned: a spawn beyond it is never refused, it is admitted immediately with a real `agent_id` and starts running on its own once a slot frees up. Whether that is one `spawn_agent` call too many or a wide `spawn_agents` batch, the extra agents wait their turn rather than being turned away. The pool is per-run, not global: concurrent sessions never contend with each other over it, so this setting only ever bounds the width of a single session's own fan-out. Raise it when a workload's natural fan-out regularly exceeds the default, for example a wide review batch, or an orchestrator whose sub-agents each spawn agents of their own, so more of it runs in parallel instead of waiting its turn. Lower it, as in the first example below, when a deployment's own resources (LLM gateway concurrency, host CPU/memory) are the binding constraint rather than wall-clock time.

**Example.** Limit sub-agents to a fast model and cap concurrency for a resource-constrained environment:

```json
{
  "agent": {
    "max_concurrent": 5,
    "default_sub_model": "anthropic/claude-haiku-4-5",
    "allowed_models": ["anthropic/claude-haiku-4-5", "anthropic/claude-sonnet-4-6"]
  }
}
```

**Example.** Raise concurrency for a workload with wide, genuinely parallel fan-out:

```json
{
  "agent": {
    "max_concurrent": 50
  }
}
```

At the maximum depth, `spawn_agent` is removed from the tool schema entirely, so the model cannot nest further even if it tries.

---

> [!NOTE] How it works internally
> See [Architecture Overview → Sub-agents and the hypervisor](core-orchestration.md#sub-agents).
