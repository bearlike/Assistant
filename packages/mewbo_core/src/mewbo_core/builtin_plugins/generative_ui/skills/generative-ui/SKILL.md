---
name: generative-ui
description: Use when presenting information already in hand as a small, static structured panel in the conversation — status, results, comparisons, or a compact summary — rather than prose, a markdown table, a widget, or an app.
requires-capabilities: [generative_ui]
---

# Generative UI panels

`present_ui` displays a static, allowlisted, no-code panel. Call it directly; it
is not a widget or an app.

## Choose the smallest surface

| Need | Use |
|---|---|
| An explanation, conclusion, or short list | Plain prose |
| A small comparison that reads well as text and needs no visual grouping | A markdown table |
| Information already in hand that benefits from status, cards, grouped fields, or a compact visual summary | `present_ui` |
| A chart, interactive controls, or a custom interactive display | Delegate to `st-widget-builder`; its Streamlit widget runs code in a browser sandbox |
| A durable surface with its own data and pipelines | Delegate to `app-builder` |

A panel lays out known information with fixed components. It does not fetch data,
run code, accept input, or become a durable application.

## The call

Three arguments, all top level. `root` is a **list**; there is no wrapper object
around it.

```json
{
  "summary": "Release checks for main",
  "root": [
    {"component": "Heading", "value": "Release checks", "level": 2},
    {
      "component": "Card",
      "title": "Current status",
      "children": [
        {"component": "Badge", "label": "Ready", "status": "success"},
        {
          "component": "KeyValue",
          "items": [
            {"label": "Tests", "value": "Passed"},
            {"label": "Review", "value": "Complete"}
          ]
        },
        {"component": "Alert", "body": "No remaining blockers.", "variant": "info"}
      ]
    }
  ]
}
```

Every node is **flat**: `component` names the type, and that type's fields sit
beside it on the same object. Only `Card` and `Stack` accept `children`.

## Component vocabulary

**The `present_ui` description carries it, flat, and that copy is the only one.**
It is derived from the same models that validate your call, so it lists every
component with its required and optional fields and cannot be out of date. Read
it there rather than expecting a list here — a second copy in this file could
only ever be older.

Two shapes the flat list cannot express: a `KeyValue` item is
`{"label": ..., "value": ...}`, and a `Table` row is a list of cells exactly as
long as `columns`. Keep panels small enough to take in at a glance.

## Do not reverse-engineer the shape

Everything the call needs is above. Traced sessions have spent four consecutive
rejected calls rediscovering that `root` is a list, and one model announced a
shape it had already disproved and sent it anyway. If a call is rejected, read
the returned error — it states the correct shape and every component's fields —
and fix that one thing. Do not probe with a test panel; a rejected call costs a
step and a successful probe still shows the user a panel they did not ask for.

The names in the schema's `$defs` are the component names themselves (`Alert`,
`Card`); there is nothing else to look up, and they are not tools — searching for
them finds nothing.

## Delegate widgets without prescribing them

For a widget, delegate with `spawn_agent(agent_type="st-widget-builder", ...)`.
Give its task only the user's purpose and the path plus field names of the data it
needs. Do not prescribe layout, sections, controls, component types, chart axes,
colours, themes, or implementation details; the sub-agent owns the runnable
Streamlit application. When its `widget_ready` event arrives, the widget is
already visible — do not recreate it as a panel or invent a link to it.

For an app, delegate to `app-builder` with the user's intent and workspace choice.
It owns the durable data surface and pipelines.

## Build rich panels in small steps

One monolithic tree is the shape most likely to arrive corrupted. Present a
small skeleton first — a heading and a `Card` or `Stack` carrying an `id` — then
grow it call by call: `operation="append"` adds nodes to the panel (or inside
the container named by `target`), and `operation="update"` with `target`
replaces that one container. Each result reports the panel's current size and
the container ids you can address next.

## After presenting

`present_ui` emits the panel but does not end the turn: continue the task or give
the normal closing reply. What to put in that reply — and why restating the panel
is wasted — is in the tool's own description; it is not repeated here.

The tool result returns a panel id. Pass it as `ui_id` in a later call to
address that panel — replace it wholesale, or extend it as above — instead of
adding another one below it.
