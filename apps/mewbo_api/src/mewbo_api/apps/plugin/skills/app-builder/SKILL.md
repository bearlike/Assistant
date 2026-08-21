---
name: app-builder
description: Use when the user asks to build, create, or set up an app (a durable data app with a dashboard and background pipelines) — not a one-off widget or chart. Teaches how to delegate app-building to the app-builder sub-agent.
agent: app-builder
requires-capabilities: [apps]
---

# app-builder

`app-builder` is a sub-agent — delegate to it via `spawn_agent`. Do not design the data model, write the frontend, or author pipelines yourself.

## How to invoke

```python
spawn_agent(
  agent_type="app-builder",
  task="<intent + workspace choice — see below>",
  acceptance_criteria="app_ready event emitted",
)
```

## What goes in the task

The sub-agent designs the collections, frontend, and pipelines itself. It handles both live/render apps and periodic/collection-backed apps. Worked pipeline shapes live at `${CLAUDE_PLUGIN_ROOT}/examples/recipes/`.

**Your task gives it only two things:**

1. **Intent** — what the user wants the app to do, in their own terms (what it tracks, who it's for, how often it should refresh).
2. **Workspace choice** — which workspace the app's agents anchor to: `own` (a fresh private workspace for this app) or `shared` (an existing one, named). This is the one structural decision the user makes at creation; pass it through, don't invent it.

```python
# Correct — intent + workspace, no layout
task=(
  "Build an app that organizes my inbox into actionable task groups every morning "
  "and shows them on a dashboard. Workspace: own."
)

# Wrong — prescribing the data model or the UI
task=(
  "Create two collections `emails` and `groups`, a cron pipeline, and a Streamlit "
  "page with a sidebar and three tabs..."
)
```

**Do not prescribe** collection schemas, the frontend layout, page structure, or pipeline mechanics. The sub-agent owns those decisions — prescribing them fights its judgment and produces a worse app.

## After the sub-agent finishes

The app is live the moment `app_ready` fires. Reply with one sentence — "Done, your app is live." Don't describe what it shows; the user can open it.

## What NOT to do

- Do not write frontend code, collection schemas, or pipeline prompts yourself.
- Do not call `activate_skill("app-builder")` — that does nothing; use `spawn_agent`.
- Do not describe the app's layout or data model in the task — hand off intent and let the sub-agent design it.
