---
name: st-widget-builder
description: Use when the user asks to render, build, visualise, mock up, or show a widget (chart, dashboard, card, table, graph) in the Meeseeks Console. Teaches how to delegate the work to the st-widget-builder sub-agent instead of writing HTML or inline code yourself.
agent: st-widget-builder
requires-capabilities: [stlite]
---

# st-widget-builder

`st-widget-builder` is a sub-agent — delegate to it via `spawn_agent`. Do not write widgets yourself.

## How to invoke

```python
spawn_agent(
  agent_type="st-widget-builder",
  task="<data hand-off + purpose — see rules below>",
  acceptance_criteria="widget_ready event emitted with app.py and data.json"
)
```

## What goes in the task

The sub-agent has a component catalog and designs the layout itself. **Your task must contain only two things:**

1. **Purpose** — one sentence: what does the user want to see?
2. **Data** — the file path + field names the widget needs.

```python
# ✓ Correct — data + purpose only
task=(
  "Show the current AAPL stock price and recent price trend. "
  "Data: /tmp/aapl_data.json — fields: quote.price, quote.change, "
  "quote.pct_change, quote.open, quote.high, quote.low, quote.volume, "
  "daily[].date, daily[].close (30 days, newest first)."
)

# ✗ Wrong — prescribing layout (forces sub-agent away from its catalog)
task=(
  "Build a header card with the price in large text, then a row of 5 "
  "metric cards, then a green line chart titled '30-Day Price History'. "
  "Use a dark theme with Altair."
)
```

**Do not describe** layout, sections, component types, chart axes, colors, or themes. The sub-agent's catalog (`StockTickerCard`, `GitHubRepoCard`, `SearchResultCard`, `PlantUMLCard`, …) already handles these consistently. Prescribing layout bypasses the catalog and forces custom code that looks worse.

## Data hand-off

Write structured data to a file before spawning — never paste raw JSON into the task string:

```bash
gh search repos --json name,description,stargazerCount,language,url \
    --limit 10 "topic:streamlit" > /tmp/meeseeks_gh_results.json
```

Then pass the path and field names in the task. The sub-agent reads, transforms, and discards the rest.

## After the sub-agent finishes

The widget is already visible the moment `widget_ready` fires. Reply with one sentence — "Done — the widget is live." Do not describe what the widget shows; the user can see it.

## What NOT to do

- Do not write `.html`, `.css`, or Python yourself
- Do not call `activate_skill("st-widget-builder")` — that does nothing; use `spawn_agent`
- Do not describe the visual layout in the task — it defeats the catalog and bloats the widget
