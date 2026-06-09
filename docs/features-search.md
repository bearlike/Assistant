# Agentic Search

<div style="display: flex; justify-content: center;">
  <img src="../mewbo-search-01-landing.jpg" alt="The Agentic Search landing page in the Mewbo Console, with a question box scoped to a workspace, connected-source chips, and a grid of workspaces such as Engineering docs, Support intel, and Research library" style="width: 100%; max-width: 960px; height: auto;" />
</div>

Ask a question in plain English and Mewbo searches across everything you've connected. A coordinating agent decomposes the question, routes each part to the right sources via the Source Capability Graph, fans out probe agents to execute the retrieval, and synthesises one ranked answer with citations and a full agent trace.

---

## Workspaces scope the search

A **workspace** is a named bundle of connected sources for one topic. *Engineering docs* might point at your repos, RFCs, and architecture pages; *Support intel* at customer tickets, Slack threads, and public issues; *Research library* at papers and reading lists. Each workspace shows the MCP sources wired into it, and every question runs against the workspace you pick. A query about a customer issue won't trawl your design system, and vice versa.

Spin up a new workspace whenever you have a new question domain: name it, choose which MCP servers it can reach, and start asking.

---

## One question, parallel probe agents

<div style="display: flex; justify-content: center;">
  <img src="../mewbo-search-02-results.jpg" alt="An Agentic Search results page showing a synthesised answer with an 86% confidence bar and citations, source-type filters, ranked result cards from GitHub, Slack, and Linear, and a right rail with an agent trace, related questions, and people" style="width: 100%; max-width: 960px; height: auto;" />
</div>

A single `scg-search` agent handles the full query lifecycle. It decomposes the question into sub-queries, calls `scg_route` to find the highest-ranked pathways through the Source Capability Graph, and spawns a bounded set of probe agents (each scoped to one pathway) to execute the actual retrieval in parallel. Because it runs on the same hypervisor as every other Mewbo task, the fan-out is observable, steerable, and bounded. A typical query resolves in seconds.

---

## How the Source Capability Graph works

The **Source Capability Graph (SCG)** is a reachability index: a graph of what each connected source *can answer*, built from its schemas and tool definitions. No credentials or record values enter the graph. It exists solely to route queries to the right capabilities before any data is fetched.

```mermaid
flowchart LR
    Q([Query]) -->|"embed"| ANN["Cosine ANN\nover capabilities"]
    MEM[("Learned\nmemory")] -->|"routing hints"| ANN
    ANN --> EXP["One-hop\ngraph expansion"]
    EXP --> REC["Ranked RouteRecipes"]
    REC --> P1["Probe agent"]
    REC --> P2["Probe agent"]
    REC --> PN["..."]
    P1 & P2 & PN --> SYN["Synthesis"]
    SYN -->|"deposit facts"| MEM
```

### What the graph contains

The SCG has five node types and eight edge types.

**Nodes:**

| Node | What it represents |
|---|---|
| `source` | A connected MCP server or API |
| `entity_type` | A schema type the source exposes (Jira Issue, Linear Ticket, database table) |
| `field` | An individual property on an entity type |
| `capability` | An executable operation: an MCP tool, an OpenAPI endpoint, a database procedure |
| `route_recipe` | A precomputed, ordered pathway through one or more capabilities that can answer a class of question |

**Edges:**

| Edge | Meaning |
|---|---|
| `HAS_ENTITY` | Source exposes this schema type |
| `HAS_FIELD` | Entity type has this field |
| `SUPPORTS_QUERY` | Capability accepts this field as input |
| `PRODUCES` | Capability returns this field in its output |
| `CONSUMES` | Capability chains into another (producer output matches consumer input) |
| `RESOLVES_TO` | Two entity types from different sources describe the same concept |

Node identities are content-addressed: `sha1(source_key | node_kind)[:16]`. Re-indexing a source produces stable, idempotent IDs.

```mermaid
flowchart LR
    SRC([source])
    ET([entity_type])
    F([field])
    CAP([capability])
    RR([route_recipe])

    SRC -->|"HAS_ENTITY"| ET
    ET -->|"HAS_FIELD"| F
    ET -->|"RESOLVES_TO"| ET2([entity_type])
    SRC -->|"exposes"| CAP
    CAP -->|"SUPPORTS_QUERY"| F
    CAP -->|"PRODUCES"| F
    CAP -->|"CONSUMES"| CAP2([capability])
    CAP -->|"builds"| RR
```

### How a source is indexed

Adding a source triggers a five-phase map pipeline:

1. **Connect**: resolve and authenticate the connector descriptor
2. **Introspect**: fetch the raw schema: OpenAPI document, MCP tool list, or SQL introspection
3. **Parse**: dispatch to the provider to emit capability nodes, entity-type nodes, field nodes, and their edges. An OpenAPI source produces one capability node per `operationId`; an MCP tool list produces one capability node per tool.
4. **Link**: run `TypeAligner` across all sources to emit weighted `RESOLVES_TO` edges where schema types correspond (see [Cross-source type alignment](#cross-source-type-alignment) below)
5. **Finalize**: embed every node with the same LiteLLM-backed embedding model used by the Agentic Wiki; compute `CONSUMES` edges by matching capability output field names to input field names across sources

The embedding step is best-effort. If no embedding backend is configured the SCG routes queries on graph structure alone.

### How queries are routed

Query routing is a **zero-LLM operation** inside `scg_route`:

1. Embed the query with the same model used at index time
2. Run a brute-force cosine ANN over all stored capability and entity-type embeddings
3. Expand one hop along all edge types in both directions (outbound + one-hop reverse lookup)
4. Score each candidate: `cosine_similarity(query, node) + edge_weight`
5. Return the top-k ranked `RouteRecipe` objects, each an ordered sequence of source steps

Each RouteRecipe becomes the brief for one probe agent. The probe is granted only the tools listed in its recipe, so it cannot wander to unrelated sources. The coordinating agent collects all probe results via `check_agents`, then synthesises them into one cited answer.

> [!NOTE] Scale path
> The current brute-force cosine pass is designed with a documented upgrade seam to Personalised PageRank at scale. The calling interface does not change; only the ranking kernel is swapped in.

### Cross-source type alignment

At map time, `TypeAligner` compares entity-type nodes across sources and emits weighted `RESOLVES_TO` edges. Alignment is heuristic-first, LLM-assisted only in the ambiguous band:

| Field-name Jaccard overlap | Behaviour |
|---|---|
| ≥ 0.6 (confident) | Edge emitted on heuristic alone |
| 0.15–0.6 (ambiguous) | One LLM call adjudicates; edge emitted only on affirmation |
| < 0.15 | Abstain; no edge emitted |

The Jaccard score compares field names between two entity types. An exact name match between the two entity types adds a +0.2 bonus on top. `RESOLVES_TO` edges are weighted hypotheses, not hard joins. The router uses them to widen the probe scope across sources that describe the same concept. The correspondence is probabilistic, not a hard join.

This is what lets a query about task ownership route to both Jira and Linear without any manual configuration, because their `assignee` fields overlap above the confidence threshold.

### Entity resolution across sources

`TypeAligner` establishes probabilistic schema correspondences at map time. At query time, a second, deeper resolution step runs: `ScgAnchorResolver` implements the wiki's `StructureProvider` protocol, making every capability node and entity-type node in the SCG a participant in the same `ResolutionLadder` used by the Agentic Wiki to resolve code symbols into named concepts.

In practice this means three things for your consumers:

- A search over "who is responsible for the billing service" does not pattern-match on the string "responsible." It resolves through the entity layer to the abstract `owner` concept, which may surface as `assignee`, `maintainer`, `responsible_team`, or `owner` depending on the source. Entity resolution finds all of them.
- "Jira Issue" and "Linear Ticket" are not just similar by field-name overlap. Once `TypeAligner` emits a `RESOLVES_TO` edge and the entity layer anchors both to the same abstract concept, queries that touch either source automatically reach both, without the probe having to enumerate individual field names.
- Route recipes are assembled from resolved entity concepts, not raw schema fields. A recipe for "find open issues assigned to a user" works across any tracker connected to the workspace, not just the one it was built from.

### The shared multiplex graph

The SCG does not operate as an isolated reachability index. It is a tenant of the same three-layer multiplex graph that powers the Agentic Wiki, each layer holding different knowledge, all stored in one place and queryable together.

| Layer | What search stores here | What the wiki stores here |
|---|---|---|
| **Schema** | Capability nodes, entity-type nodes, and field nodes for all connected sources | AST symbols, imports, and call graphs from indexed codebases |
| **Entity** | Abstract concepts resolved across sources via `ScgAnchorResolver` | Named entities extracted by GraphRAG enrichment (services, modules, owners) |
| **Memory** | Reachability facts deposited after each search run, anchored to capability and entity-type nodes | Q&A findings deposited by `QaMemoryDepositor`, anchored to code symbols |

When a project is both wiki-indexed and part of a search workspace, the memory layers share the same store. A Q&A session that discovers "the `orders` module owns all purchase state" can surface during a search about purchase flows. A search run that discovers "the `catalog-api` server only returns published items by default" can inform a subsequent wiki answer about catalog data. The layers cross-pollinate without any explicit wiring.

Before each query, the top-k relevant memory notes are retrieved via vector search and surfaced to `scg_route`, biasing routing toward pathways that have produced results and away from dead ends already discovered. The memory layer grows with use. No manual curation is required.

---

## Search tiers

Every search runs at one of three tiers, selectable per query:

| Tier | Sub-query decomposition | Probe fan-out | Best for |
|---|---|---|---|
| **Fast** | 1 | 2 | Quick lookups; known-answer retrieval |
| **Auto** (default) | 2–3 | 3 | General multi-source questions |
| **Deep** | 3–5 | 5 | Exhaustive research; cross-source synthesis |

Tiers control decomposition depth and probe fan-out only. There are no verification rounds and no consensus voting. Each probe agent queries its connector directly and returns what it finds. Connector returns are ground truth: if a pathway returns data, the answer is grounded in it; if it returns nothing, the pathway is marked as a miss in the trace.

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

- **Agent trace**: which sources were queried and which returned a hit, so you can see the search actually ran end to end.
- **Related questions**: the obvious next questions, one click away.
- **People**: who authored, merged, or reported the artefacts behind the answer, pulled straight from the sources.

---

## Programmatic access via MCP

Agentic Search is accessible through the [MCP server](clients-mcp.md), so external agents and automated pipelines can run searches without the console:

| Tool | What it does |
|---|---|
| `list_search_workspaces` | List your saved workspaces. Returns each workspace's id, name, and connected sources. |
| `search` | Run a query against a workspace and receive a cited answer. Pass the workspace id or name; optionally scope to a specific project. |
| `get_search_run` | Fetch the result of a prior search run. Useful for long-running searches or replaying past results. |

Results come back at two detail tiers: **`answer`** (synthesis plus a compact result index, the default) and **`full`** (adds per-result snippets and entity insights).

> [!TIP]
> When you need the answer in a validated JSON structure rather than prose, the [Structured Outputs](features-structured-outputs.md) endpoint runs an agentic session grounded in a search workspace and emits a machine-readable object matching your schema. Useful for automated pipelines.

---

## Availability

Agentic Search lives in the **Mewbo Console**, reachable from the top navigation next to Tasks and Wiki. It reads the MCP servers you've already configured (the same connections used everywhere else in Mewbo) and groups them into workspaces.

> [!NOTE] Going deeper
> Search reuses the same primitives as the rest of the engine. See [External Tools (MCP)](features-mcp.md) for how connected sources are configured, and [Sub-agents](features-agents.md) for the parallel fan-out and the hypervisor that bounds it.
