# Panels

## Lay out an answer inline

Ask Mewbo for a status board, a comparison or a set of results and it draws a **panel**, a small structured block laid out inline in place of prose. The model composes it from a fixed set of components, and the console renders it through [`GenerativeUICard`](repo:apps/mewbo_console/src/components/GenerativeUICard.tsx).

Nothing is built and no code runs. Presenting a panel is one step in the middle of a run, so the agent keeps working and still writes its closing reply.

## When to reach for a panel

Panels, widgets and apps all put something visual in front of you, at very different costs.

| Reach for | When |
|---|---|
| A **panel** | The answer is already in hand and only needs laying out. Status boards, comparison tables, result lists, a config summary. |
| A [widget](widgets.md) | The result wants a chart or interactive controls, built as a small Streamlit app that runs in a browser sandbox. |
| A [Mewbo App](../apps/index.md) | The surface should outlive the conversation, with its own manifest, data store, and pipelines. |

A panel is a summary taken in at a glance, not a document.

## The component vocabulary

A panel is a tree built from exactly eleven components, each with its own typed fields, defined once in [`nodes.py`](repo:packages/mewbo_core/src/mewbo_core/builtin_plugins/generative_ui/nodes.py).

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

The list is a contract, not a menu. The model fills in fields on these names and never writes markup, styling, or code. A name outside the list renders as a placeholder row rather than breaking the conversation, and a link is accepted only for `http`, `https`, and `mailto` destinations. A panel is bounded to 200 nodes and eight levels of nesting, so a model that tries to render a whole report gets a correction it can act on, not a wall of output.

## Panels read the same everywhere

Every panel ships with a rendering in plain text, computed when it is presented. A surface that cannot draw the tree shows that text instead, so the terminal and the MCP server still carry the contents. The agent is instructed not to repeat them in its reply.

That is what makes the vocabulary portable. The eleven components describe a panel independently of who renders it, so a new chat client renders against this tree rather than needing a format of its own.

## Refining a panel in place

Each panel gets an id when it is presented, and the model may author a readable one of its own. Presenting again with that id replaces the panel where it already sits, instead of a near duplicate stacking up below it. A long run that keeps narrowing an answer leaves one current panel behind, not a pile of drafts. A panel can also grow in place: a `Card` or `Stack` given an `id` becomes addressable, and later calls append into it or update it by name, so a rich panel is assembled from several small calls.

## Turning it on

Panels are gated on the `generative_ui` capability, which a client advertises with the `X-Mewbo-Capabilities` request header. The web console sends it on every request, so panels work there with no configuration. To advertise it from your own client, see [Building a Client](../api/building-a-client.md#client-capability-negotiation).

## Next steps

- [Widgets](widgets.md). Interactive Streamlit apps for results that need charts or controls.
- [Sessions](sessions.md). The conversation a panel is rendered into.
- [Building a Client](../api/building-a-client.md). Advertise `generative_ui` from your own client.
