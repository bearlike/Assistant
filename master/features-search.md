# Agentic Search

<div style="display: flex; justify-content: center;">
  <img src="../assets/img/mewbo-search-01-landing.jpg" alt="The Agentic Search landing page in the Mewbo Console: a question box scoped to the OSS Repo Scout workspace on the Fast tier, chips showing 6 of 7 sources mapped, graph node-edge counts, and memory notes, above a grid of saved workspaces (Knowledge graph, SideStage Ops, OSS Repo Scout)" style="width: 100%; max-width: 960px; height: auto;" />
</div>

Ask a question in plain English and Mewbo searches across everything you've connected. A coordinating agent decomposes the question, routes each part to the right sources via the [Source Capability Graph](features-search-scg.md), fans out probe agents to execute the retrieval, and synthesises one ranked answer with citations and a full agent trace.

---

## Workspaces scope the search

A **workspace** is a named bundle of connected sources for one topic. *Engineering docs* might point at your repos, RFCs, and architecture pages; *Support intel* at customer tickets, Slack threads, and public issues; *Research library* at papers and reading lists. Each workspace shows the MCP sources wired into it, and every question runs against the workspace you pick. A query about a customer issue won't trawl your design system, and vice versa.

Spin up a new workspace whenever you have a new question domain: name it, choose which MCP servers it can reach, and start asking.

### Edit the purpose, re-index the graph

Every workspace card carries an edit (pencil) button. It opens the workspace's name, its source selection, and a **Purpose & instructions** field. That text is not decoration. It codifies what the workspace's graph is for, and it seeds the enrichment step that runs when sources are mapped. Save a meaningful change (the purpose text, the description, or the source selection) and Mewbo re-maps and re-enriches the workspace's mapped sources in the background; the console confirms with a re-index toast. A name-only edit changes nothing in the graph and stays quiet.

### See what a workspace knows

Each card also has a graph button. It opens the workspace's capability graph in a dialog, served by [GET /api/agentic_search/workspaces/{workspace_id}/graph](endpoint:GET /api/agentic_search/workspaces/{workspace_id}/graph). The view is layered, with a toggle per layer:

- **Schema**: capability, entity-type, and field nodes from the workspace's mapped sources.
- **Memory**: the learned connector notes deposited by past runs.
- **Entity**: abstract concepts resolved across sources.

Sources you enabled but have not yet mapped appear as ghost nodes with a hint to map them, so a half-configured workspace is visible at a glance instead of silently smaller.

<div style="display: flex; justify-content: center;">
  <img src="../assets/img/mewbo-search-03-capability-graph.jpg" alt="A workspace capability graph dialog for OSS Repo Scout showing 86 nodes and 95 edges in a force-directed view, with a node filter, a Capability/Capabilities/Memory layer legend, and a re-layout control" style="width: 100%; max-width: 960px; height: auto;" />
</div>

---

## One question, parallel probe agents

<div style="display: flex; justify-content: center;">
  <img src="../assets/img/mewbo-search-02-results.jpg" alt="An Agentic Search results page: a synthesised answer to a question about popular agentic coding tools on the Fast tier, with an 86% High confidence bar, All/Docs/Code source-type filters, ranked GitHub result cards comparing agentic coding tools, each showing stars, language, and licence, and a right rail with the agent trace and capability graph" style="width: 100%; max-width: 960px; height: auto;" />
</div>

A single `scg-search` agent handles the full query lifecycle. It decomposes the question into sub-queries, calls `scg_route` to find the highest-ranked pathways through the Source Capability Graph, and spawns a bounded set of probe agents (each scoped to one pathway) to execute the actual retrieval in parallel. Because it runs on the same hypervisor as every other Mewbo task, the fan-out is observable, steerable, and bounded. A typical query resolves in seconds.

> [!NOTE] How the routing works
> The Source Capability Graph is the reachability index that decides which sources can answer each sub-query and how to chain them, all before any data is fetched. See [The Source Capability Graph](features-search-scg.md) for the full mechanics: indexing, zero-LLM routing, cross-source entity resolution, and the shared multiplex graph.

---

## Search tiers

Every search runs at one of three tiers, selectable per query. The tier is the run's single knob. It sets the decomposition budget, the probe fan-out, and the model the run thinks with.

| Tier | Sub-query decomposition | Probe fan-out | Default model | Best for |
|---|---|---|---|---|
| **Fast** | 1 | 2 | `openai/gpt-oss-120b` | Quick lookups; known-answer retrieval |
| **Auto** (default) | 2–3 | 3 | `openai/gpt-oss-120b` | General multi-source questions |
| **Deep** | 3–5 | 5 | `openai/gpt-oss-120b` | Exhaustive research; cross-source synthesis |

The model mapping lives at `scg.traversal.tier_models` (keys `fast`, `auto`, `deep`) and is editable in Settings like any other config key. Probe agents inherit the session model, so one tier choice moves the whole run, coordinator and probes alike. A blank mapping or an unrecognised tier falls back to `llm.default_model`, never an error. Where a request offers an explicit `model` override, it wins over the tier map.

Tiers add no verification rounds and no consensus voting. Each probe agent queries its connector directly and returns what it finds. Connector returns are ground truth: if a pathway returns data, the answer is grounded in it; if it returns nothing, the pathway is marked as a miss in the trace.

---

## A synthesised answer, with receipts

The top of every result is a **Synthesis** card: a direct, written answer to your question rather than ten blue links. It carries:

- **Inline citations**: each claim links to the source result it came from.
- **A confidence score**: reflects how many independent pathways returned corroborating evidence, weighted by the relevance scores returned by each probe.
- **Ask a follow-up**: keep pulling the thread without re-scoping; the workspace and context carry over.

Below it, the underlying **results** are listed in rank order (a merged PR, a Slack thread, a tracker issue), each with its source, status, and a snippet. Filter the list by type with one click: **Docs**, **Code**, **Threads**, **Design**, **Tickets**, or **Web**.

> [!TIP]
> When a wiki-indexed project is part of the workspace, search and wiki share the same multiplex graph. Search results that touch that codebase automatically draw on the wiki's entity and memory layers, surfacing what a module does, how subsystems relate, and what past Q&A sessions have established, alongside the raw connector results. No extra setup is needed.

---

## See how it got there

Agentic Search is transparent by design. Alongside each answer:

<div style="display: flex; justify-content: center;">
  <img src="../assets/img/mewbo-search-04-agent-trace.jpg" alt="An Agentic Search result with the Agent trace rail expanded, showing the scg-search coordinator and its claude-sonnet-4-6 probe agents, beside a synthesised answer and a ranked table of matching repositories" style="width: 100%; max-width: 960px; height: auto;" />
</div>

- **Agent trace**: which sources were queried and which returned a hit, so you can see the search actually ran end to end.
- **Related questions**: the obvious next questions, one click away.
- **People**: who authored, merged, or reported the artefacts behind the answer, pulled straight from the sources.

### Share a run with a link

Every run has a stable, shareable URL: `/search?ws=<workspace>&run=<run>`. Opening it loads the saved run directly: the synthesis, results, and trace render from a single durable snapshot, then live updates attach if the run is still in flight. The link is deterministic and multi-user: it resolves the same run for anyone with access, survives a server restart, and re-opens to the exact answer (linked to its underlying session for auditing). A link to a run that no longer exists returns a clean "not found" rather than an error page.

---

## Programmatic access via MCP

Agentic Search is accessible through the [MCP server](clients-mcp.md), so external agents and automated pipelines can run searches without the console:

| Tool | What it does |
|---|---|
| `list_search_workspaces` | List your saved workspaces. Returns each workspace's id, name, and connected sources. Pass an optional query string to filter by name, description, or past-query text. |
| `search` | Run a query against a workspace and receive a cited answer. Pass the workspace id or name; optionally scope to a specific project. |
| `get_search_run` | Fetch the result of a prior search run. Useful for long-running searches or replaying past results. |

Results come back at two detail tiers: **`answer`** (synthesis plus a compact result index, the default) and **`full`** (adds per-result snippets and entity insights).

> [!TIP]
> When you need the answer in a validated JSON structure rather than prose, the [Structured Outputs](api/structured-outputs.md) endpoint runs an agentic session grounded in a search workspace and emits a machine-readable object matching your schema. Useful for automated pipelines.

---

## Availability

Agentic Search lives in the **Mewbo Console**, reachable from the top navigation next to Tasks and Wiki. It reads the MCP servers you've already configured (the same connections used everywhere else in Mewbo) and groups them into workspaces.

### Enabling search

Orchestrated search ships disabled. To turn it on:

1. Enable the **SCG** switch in Settings (the `scg.enabled` key).
2. Open **Sources** on the Search landing page and **map** each source you want searchable. Mapping introspects the source's schema and builds its capability subgraph. Progress streams live and survives a page reload.
3. Pick a **tier** (Fast / Auto / Deep) next to the search bar; the default comes from `scg.traversal.default_tier`.

Until search is enabled and at least one source is mapped, queries run against bundled demo fixtures so you can explore the surface. The switch to real orchestrated runs takes effect on the next query, no restart required.

> [!NOTE] Going deeper
> For the routing engine under the hood, see [The Source Capability Graph](features-search-scg.md). Search reuses the same primitives as the rest of the engine: [External Tools (MCP)](features-mcp.md) for how connected sources are configured, and [Sub-agents](features-agents.md) for the parallel fan-out and the hypervisor that bounds it.
