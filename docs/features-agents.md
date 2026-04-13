# Sub-agents

Meeseeks can spawn child agents to work on independent subtasks in parallel. The root agent acts as an orchestrator: it delegates bounded work to sub-agents, monitors their progress, collects structured results, and synthesises a final answer. Sub-agents inherit the parent's permission policy, tool registry, and session context, so they can pick up a task and run with it without re-authorising every tool call.

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

### Blocking vs. non-blocking

**When the root agent spawns, the call is non-blocking.** The call returns immediately with an ID:

```json
{
  "agent_id": "a1b2c3d4-...",
  "status": "submitted",
  "task": "Run the full test suite...",
  "message": "Agent spawned. Use check_agents to monitor progress and collect results."
}
```

The agent runs in the background. Use `check_agents` to poll or wait for completion.

**When a sub-agent itself spawns a deeper agent, the call is blocking.** The deeper call waits for the child to finish and returns the result inline, so a mid-level agent reads the outcome the moment it is available.

Sub-agents run until the model returns a text response without any more tool calls — that is natural completion. There is no hard step limit; safety comes from per-call timeouts, stall detection, and the session-wide step budget.

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

A steering message is queued and delivered to the agent between its next tool steps — it never interrupts an in-flight tool call.

---

## What a sub-agent returns

When a sub-agent finishes, its result is a structured object:

| Field | Type | Description |
|---|---|---|
| `content` | string | Primary output text |
| `status` | string | `completed`, `failed`, `partial`, or `cannot_solve` |
| `steps_used` | integer | Number of tool steps executed |
| `summary` | string | Compressed summary (≤ 500 chars) for the parent's context |
| `warnings` | array | Non-fatal issues encountered |
| `artifacts` | array | File paths the agent touched |

The `summary` is designed to be short enough that the root agent can keep many completed children in context without blowing up the window; `content` is available when you need the full output.

---

## Configuration

All keys live under `agent` in [`configs/app.json`](configuration.md#agentconfig).

| Key | Type | Default | Description |
|---|---|---|---|
| `agent.enabled` | boolean | `true` | Enable or disable sub-agent spawning |
| `agent.max_depth` | integer | `5` | Maximum nesting depth (minimum `1` = no sub-agents) |
| `agent.max_concurrent` | integer | `20` | Maximum number of agents that may run at the same time |
| `agent.default_sub_model` | string | `""` | Default model for sub-agents; inherits root model when empty |
| `agent.allowed_models` | array | `[]` | Allowlist of models sub-agents may use; empty = unrestricted |
| `agent.llm_call_timeout` | float | `60.0` | Per-call timeout in seconds for a single model invocation |
| `agent.llm_call_retries` | integer | `2` | Retries on the primary model before cascading to `llm.fallback_models` |
| `agent.default_denied_tools` | array | `[]` | Tool IDs denied to all sub-agents globally |

**Example** — limit sub-agents to a fast model and cap concurrency for a resource-constrained environment:

```json
{
  "agent": {
    "max_concurrent": 5,
    "default_sub_model": "anthropic/claude-haiku-4-5",
    "allowed_models": ["anthropic/claude-haiku-4-5", "anthropic/claude-sonnet-4-6"]
  }
}
```

At the maximum depth, `spawn_agent` is removed from the tool schema entirely, so the model cannot nest further even if it tries.

---

> [!NOTE] How it works internally
> See [Architecture Overview → Sub-agents and the hypervisor](core-orchestration.md#sub-agents).
