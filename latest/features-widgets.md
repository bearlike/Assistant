# Widgets

<div style="display: flex; justify-content: center;">
  <img src="../meeseeks-console-07-widgets.png" alt="Stock ticker and GitHub repo card widgets rendered inline in the Meeseeks Console" style="width: 100%; max-width: 960px; height: auto;" />
</div>

Ask Meeseeks to visualise a result and an interactive widget appears inline in the conversation — right where the answer landed, no separate tabs, no external tools.

---

## When to reach for a widget

Widgets are most useful when a visual result communicates more than a text reply ever could:

- **Private data systems** — internal APIs, databases, and pipelines that lack a search or reporting interface can surface results as scannable cards or charts on demand.
- **Research and analysis** — repository metrics, search results, financial positions, or any multi-field dataset formatted as cards the whole team can read at a glance.
- **Reference artefacts** — cheat sheets, comparison tables, sequence diagrams, and KPI snapshots that persist in the session for as long as you need them.

---

## How it works

Widget tasks are routed to a dedicated `st-widget-builder` sub-agent. The root agent hands off the request — along with any data it has already gathered — and the widget appears in the timeline the moment the sub-agent submits it. No intervention required.

> [!NOTE] Token cost
> The sub-agent runs in its own context, extending the session's token budget. Overhead is modest: the sub-agent draws from a pre-built component library rather than generating code from scratch.

---

## Sandboxed and persistent

Widgets run entirely in the browser inside an isolated Web Worker — no server execution, no network access, no shared state with the console or other widgets. An error in one widget cannot affect your session.

All data is baked in at build time from values the agent has already collected. No live calls are made after the widget loads. Because the state file is written to disk at creation, **widgets you built in a previous session are still there when you come back** — an exact snapshot of what the data showed at that moment. Ask for a fresh widget when the underlying data changes.

---

## Component library

The sub-agent picks from a built-in library of ready-made components — repo cards, search-result cards, stock tickers, PlantUML diagrams, and more. The library follows a drop-in convention: add a new component file to the plugin's `examples/components/` directory and the agent discovers it at the start of the next session, no configuration required.

> [!TIP] Custom component libraries
> Teams can maintain a private library of branded cards, domain-specific layouts, or internal visualisations. Because the convention is purely file-based, onboarding a new component is a single file drop.

---

## Availability

Widgets are exclusive to the **Meeseeks Console**. The console signals widget support to the API on each request; sessions from the CLI, REST API, email, and chat adapters will not see the widget-builder. The feature is on by default — no configuration needed.

> [!NOTE] Going deeper
> See [Plugins & Marketplace](features-plugins.md) for the capability model that gates the feature per surface, and [Architecture Overview](core-orchestration.md) for the session-tool protocol and component authoring contract.
