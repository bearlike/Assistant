# Agentic Wiki

<div style="display: flex; justify-content: center;">
  <img src="../mewbo-wiki-01-landing.jpg" alt="The Agentic Wiki landing page in the Mewbo Console, with a repository URL field, a Generate Wiki button, and a card for an already-indexed project" style="width: 100%; max-width: 960px; height: auto;" />
</div>

Paste a repository URL and Mewbo writes the documentation for it. The **Agentic Wiki** indexes a codebase, maps its structure into a code memory graph, and generates a navigable, grounded wiki. Every page is backed by the source files it describes. Ask a question about the repo and a sub-agent answers from that same graph: fast, authoritative, and grounded in the code itself.

---

## What you get

A generated wiki (branded **MewboWiki** inside the product) is a full documentation site for one repository:

- **An overview that reads like docs, not a file dump.** Prose pages explain the runtime model, architecture, and conventions, with flow diagrams and an *On this page* outline. Each page lists the *Relevant source files* it was written from, so every claim traces back to code.
- **An interactive knowledge graph.** The whole repository as a force-directed graph spanning three layers: structural symbols, abstract entities, and memory notes. Use the layer toggle to show one, two, or all three at once.
- **Ask MewboWiki.** An inline Q&A box on every page. Ask a question, pick a model, and a coordinating agent fans out several probe agents to explore different angles of the codebase in parallel before synthesising a single, grounded answer.
- **Copy badge.** On any wiki project's landing page, the **Copy badge** button generates a markdown snippet you can paste straight into your repository's README. It links back to the project's wiki page.

<div style="display: flex; justify-content: center;">
  <img src="../mewbo-wiki-02-overview.jpg" alt="A MewboWiki overview page showing a Grove Overview article with a runtime-model flow diagram, a left-hand page index, an On this page outline, and an Ask MewboWiki question box" style="width: 100%; max-width: 960px; height: auto;" />
</div>

---

## How indexing works

Generating a wiki is a normal Mewbo session, not a separate service: an agent owns the run, persists state as it goes, and streams progress back to the console live. The run is a fixed six-phase pipeline:

```
clone → scan → graph → plan → pages → finalize
```

1. **Clone** the repository (private repos accept an access token in the wizard).
2. **Scan** every source file.
3. **Graph** the code into a property graph: parse each file's AST with tree-sitter, then lift files, classes, functions, methods, and interfaces into nodes linked by their call, import, and definition relationships. After the structural pass, an enrichment step extracts the abstract entity layer described below.
4. **Plan** the set of pages to write.
5. **Pages:** sub-agents write each page in parallel, grounded in the scanned source.
6. **Finalize:** dedupe, attach the repository description, and publish.

The landing-page card and the indexing screen read the same progress signal, so they never disagree about which phase a run is in. As it writes, the indexer also **deposits a few durable notes** into the memory layer (described below): short, anchored facts about each subsystem. The wiki starts out already knowing the non-obvious things about the codebase.

> [!TIP] Resilient to model changes
> If the model assigned to an indexing run becomes unavailable mid-run (retired, quota-exceeded, or otherwise unreachable), Mewbo automatically switches to the next model on its fallback ladder and continues from the last checkpoint. A transparent event is logged in the console so you can see that a switch happened and why. Long indexing runs complete even when individual models go dark.

When a repository changes, **Re-index this wiki** refreshes it **on demand**. It compares what actually changed since the last run and recomputes only the affected scope: the pages, notes, and graph nodes the edit touched. It does not rebuild everything. A small change stays a small, fast update; a stale note is retired, not silently kept.

> [!NOTE] Ground the output with repo notes
> If the repository contains a `.mewbo/wiki.json` file, the indexer adopts its page plan and folds its notes into the page-writing prompts. It's the simplest way to steer which pages get written and to inject facts the code alone won't reveal.

---

## The knowledge graph

<div style="display: flex; justify-content: center;">
  <img src="../mewbo-wiki-03-graph.jpg" alt="The MewboWiki knowledge graph view rendering thousands of nodes and edges for a repository, with a legend counting files, classes, functions, methods, and interfaces" style="width: 100%; max-width: 960px; height: auto;" />
</div>

Every indexed repository ships with a **Graph** view: a live, zoomable map of the codebase built from the same index that backs the pages. Nodes are coloured by symbol type (file, class, function, method, interface) and the legend counts each. Use it to find the dense centres of a project, trace how a subsystem connects to the rest, or jump from a symbol to the page that documents it.

The graph is a **three-layer multiplex**. Use the layer toggle in the console to show one layer, two, or all three:

- **Structural (AST) layer:** files, classes, functions, methods, and interfaces as nodes; call, import, and definition relationships as edges.
- **Entity layer:** abstract entities extracted after the AST phase: the people, subsystems, components, and concepts that matter in the codebase. Each entity has a deterministic id and is grounded in real AST symbols and source prose, never in generated text. Cross-layer *anchor edges* connect entity nodes to the AST symbols they describe, so you can see exactly how a concept maps to code.
- **Memory layer:** atomic notes anchored to both structural symbols and entity nodes (described in the next section).

---

## Grounded by a code memory graph

Most documentation tools chop a repository into text and search it like prose. Mewbo builds a graph instead. As shown above, indexing lifts each file's AST into a **code property graph** (the structural layer), then adds an **entity layer** of abstract concepts extracted via a GraphRAG-grounded enrichment pass, and finally a **memory layer** of atomic notes anchored to symbols and entities. Structure, concepts, and meaning live as separate dimensions over one shared graph: a **multiplex code memory graph** that remembers both how the code is wired and what it means. It's what carries the wiki from single-file lookups to repository scale.

That graph is what makes answers fast and authoritative. A question doesn't pull a handful of look-alike snippets; it traverses the graph across **multiple hops** (a symbol, to its callers, to the module that owns them, to the note that explains why), assembling a connected, repository-scale context before the model writes a word. Keyword and semantic search seed the entry points; the memory graph expands them into the full picture. Because every note is anchored, a stale answer can't hide: when the code under a note changes, the next on-demand refresh re-checks the note and retires it if it no longer holds.

The memory **grows as the wiki is used**. The indexer seeds it while writing pages. When Ask MewboWiki finds a durable fact (something worth remembering), it **automatically deposits a note** into the memory layer: validated, condensed, anchored to the right symbols, and merged like any other insight, off the critical path so it doesn't slow the answer down. You and your agents can also **submit your own insights** explicitly: a one-line fact the code alone won't reveal, which Mewbo condenses, de-duplicates, and merges. Every future answer is built on the accumulated set.

> [!NOTE] Why multi-hop matters
> A flat search answers a cross-module question with three disconnected results. Traversing the memory graph follows real relationships in the code instead, so the answer holds together and every hop stays grounded. Semantic embeddings are an optional accelerator. Without them, keyword retrieval still seeds the graph and the wiki keeps working.

> [!TIP] Contribute an insight
> Agents on your fleet can teach the wiki as they work via the [MCP server](clients-mcp.md)'s `submit_insight` tool or the REST endpoint. Each insight is validated, condensed to an atomic note, anchored to the code it's about, and safely merged, so the knowledge compounds instead of drifting into duplicates.

### How Ask MewboWiki builds answers

When you submit a question, a coordinating agent fans out several **probe agents** in parallel. Each probe explores a different angle of the codebase using the graph. ANN-guided entry points steer each probe toward the most relevant symbols and notes. The coordinator collects their findings and synthesises them into one answer. The fan-out is bounded: if the step budget is exhausted, the coordinator wraps up with whatever was found rather than running indefinitely. Cross-module questions benefit most; the fan-out naturally pulls together evidence that a single-agent search would miss.

Every answer surfaces **inline citation chips** (`[path:line-range]` markers embedded in the answer text). Click a chip to expand a **source card** showing the actual code excerpt from that file, so you can verify every claim without leaving the wiki. A **Sources** panel below the answer also lists all wiki pages and source files that were accessed during generation.

---

## Enabling it

The wiki ships as an opt-in extra on the API server:

```bash
# Install with the wiki extras
uv sync --extra wiki

# Run the API server
uv run mewbo-api
```

The `/v1/wiki/*` routes mount only when the extras resolve; without them the server starts cleanly with the feature absent. Persisting wikis and live indexing progress use the MongoDB storage backend.

> [!NOTE] Private-repo tokens are remembered
> Access tokens entered during the indexing wizard are persisted, so re-index runs don't prompt you again. If a token is revoked or expires, the next re-index detects the failure and prompts for a replacement.

> [!NOTE] Going deeper
> The wiki runs on the same engine as everything else. See [Sub-agents](features-agents.md) for the delegation model that writes pages in parallel, and [Architecture Overview](core-orchestration.md) for the session runtime underneath.
