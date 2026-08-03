# Widgets

<div style="display: flex; justify-content: center;">
  <img src="../../assets/img/mewbo-console-07-widgets.png" alt="A trending-repositories widget rendered as a card grid inline in the Mewbo console" style="width: 100%; max-width: 960px; height: auto;" />
</div>

Ask Mewbo to visualize a result and an interactive widget appears inline in the conversation, right where the answer landed. There are no separate tabs and no external tools. The widget renders through [`StliteWidgetPanel`](repo:apps/mewbo_console/src/components/StliteWidgetPanel.tsx), mounted in the timeline by [`WidgetCard`](repo:apps/mewbo_console/src/components/WidgetCard.tsx).

## When to reach for a widget

A widget earns its place when a visual result says more than a text reply could, and when that result wants a chart or a control the reader can move. If the answer is already in hand and only needs laying out, a [panel](panels.md) shows it in a single step with nothing to build.

- **Private data systems.** Internal APIs, databases, and pipelines that have no reporting interface can surface results as scannable cards or charts on demand.
- **Research and analysis.** Repository metrics, search results, financial positions, or any multi-field dataset formatted as cards the whole team can read at a glance.
- **Reference artifacts.** Cheat sheets, comparison tables, sequence diagrams, and snapshots that persist in the session for as long as you need them.

## How it works

A widget request is routed to a dedicated sub-agent. The root agent hands off the request, along with any data it has already gathered. The widget appears in the timeline the moment the sub-agent submits it. No intervention is required.

The sub-agent runs in its own context, so it extends the session's token budget. The overhead is modest. The sub-agent draws from a pre-built component library rather than writing code from scratch.

## Sandboxed and persistent

Widgets run entirely in the browser, inside an isolated Web Worker. There is no server execution, no network access, and no shared state with the console or with other widgets. An error in one widget cannot affect your session.

All data is baked in at build time from values the agent already collected. No live calls are made after the widget loads. Because the widget's state is written to disk at creation, a widget you built in a previous session is still there when you come back. It is an exact snapshot of what the data showed at that moment. Ask for a fresh widget when the underlying data changes.

## Component library

The sub-agent picks from a built-in library of ready-made components. It includes repository cards, search-result cards, stock tickers, diagrams, and more. The library follows a drop-in convention. Add a new component file to the plugin's component directory and the agent discovers it at the start of the next session. No configuration is required.

Teams can keep a private library of branded cards, domain-specific layouts, or internal visualizations. Because the convention is file-based, onboarding a new component is a single file drop.

## Availability

Widgets are exclusive to the console. The console signals widget support to the API on each request. Sessions from the CLI, the REST API, email, and chat adapters do not see the widget builder. The feature is on by default. No configuration is needed.

The console advertises its rendering capabilities through a request header, and the API gates the widget builder on that signal. For the capability model that gates the feature per surface, see [Plugins and Marketplace](../features-plugins.md#capability-gating).

## Next steps

- [Panels](panels.md): the lighter-weight way to show structure inline, with no build step.
- [Sessions](sessions.md): the session that hosts a widget in its timeline.
- [Plugins and Marketplace](../features-plugins.md): the capability model that gates widgets per surface.
- [Get Started](index.md): launch the console and run your first session.
