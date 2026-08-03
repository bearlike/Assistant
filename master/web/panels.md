# Panels

Ask Mewbo for a status board, a comparison, or a set of results and it draws a **panel**: a small, structured block laid out inline in the conversation instead of described in prose. The model composes the panel from a fixed set of components, and the console renders it through [`GenerativeUICard`](repo:apps/mewbo_console/src/components/GenerativeUICard.tsx).

Nothing is built and no code runs. Presenting a panel is a single step in the middle of a run, so the agent keeps working and still writes its closing reply.

## When to reach for a panel

Panels, widgets, and apps all put something visual in front of you, and they cost very different amounts.

| Reach for | When |
|---|---|
| A **panel** | The answer is already in hand and only needs laying out. Status boards, comparison tables, result lists, a config summary. |
| A [widget](widgets.md) | The result wants a chart or interactive controls, built as a small Streamlit app that runs in a browser sandbox. |
| A [Mewbo App](../apps/index.md) | The surface should outlive the conversation, with its own manifest, data store, and pipelines. |

A panel is a summary a reader takes in at a glance, not a document. When the model wants to say more than that, prose or a widget is the better answer.

## The component vocabulary

A panel is a tree built from exactly eleven components. Each carries its own typed fields, defined once in [`nodes.py`](repo:packages/mewbo_core/src/mewbo_core/builtin_plugins/generative_ui/nodes.py).

| Component | What it shows |
|---|---|
| `Text` | A paragraph, optionally muted as secondary detail |
| `Heading` | A section heading at one of three ranks |
| `Card` | A titled surface grouping related nodes |
| `Stack` | A row or column of nodes, with spacing |
| `Badge` | A short status pill, coloured by meaning |
| `KeyValue` | A definition list of short label and value pairs |
| `Table` | A small tabular dataset with headers |
| `CodeBlock` | A fenced block of source or structured output |
| `Alert` | A callout drawing attention to one fact |
| `Divider` | A horizontal rule between sections |
| `Link` | A hyperlink |

The list is a contract, not a menu. The model picks from these names and fills in their fields; it never writes markup, styling, or code. A name outside the list renders as a single placeholder row rather than breaking the conversation, and a link is accepted only for `http`, `https`, and `mailto` destinations. Panels are also bounded: at most 200 nodes and eight levels of nesting, so a model that tries to render a whole report gets a correction it can act on instead of a wall of output.

## Panels read the same everywhere

Every panel ships with a plain-text rendering computed at the moment it is presented. A surface that cannot draw the tree shows that text instead, so the terminal, the MCP server, and any other non-visual client see the panel's contents rather than a note that something was presented. The agent is told not to repeat a panel's contents in its reply, because nothing is lost by not repeating them.

That is also what makes the vocabulary portable. The same eleven components describe a panel independently of who renders it, so a future chat client, a Slack integration for example, would render against this tree rather than needing a format of its own.

## Refining a panel in place

Each panel gets an id when it is presented. If the agent presents again with that id, the panel is replaced where it already sits instead of a near-copy stacking up below it. A long run that keeps narrowing an answer leaves one current panel behind, not a pile of drafts.

## Turning it on

Panels are gated on the `generative_ui` capability, which a client advertises with the `X-Mewbo-Capabilities` request header. The web console sends it on every request, so panels work in the console with no configuration.

Advertising the capability and rendering the result are one decision. A client that advertises without being able to draw a panel spends a step producing something nobody can read, so send the capability only once you render it. For the header itself and the rest of the negotiation, see [Building a Client](../api/building-a-client.md#client-capability-negotiation).

## Next steps

- [Widgets](widgets.md): interactive Streamlit apps for results that need charts or controls.
- [Sessions](sessions.md): the conversation a panel is rendered into.
- [Building a Client](../api/building-a-client.md): advertise `generative_ui` from your own client.
