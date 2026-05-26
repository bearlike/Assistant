# Agentic Wiki

<div style="display: flex; justify-content: center;">
  <img src="../mewbo-wiki-01-landing.jpg" alt="The Agentic Wiki landing page in the Mewbo Console, with a repository URL field, a Generate Wiki button, and a card for an already-indexed project" style="width: 100%; max-width: 960px; height: auto;" />
</div>

Paste a repository URL and Mewbo writes the documentation for it. The **Agentic Wiki** indexes a codebase, maps its structure into a code memory graph, and generates a navigable, grounded wiki. Every page is backed by the source files it describes. Ask a question about the repo and a sub-agent answers from that same graph: fast, authoritative, and grounded in the code itself.

---

## What you get

A generated wiki (branded **MewboWiki** inside the product) is a full documentation site for one repository:

- **An overview that reads like docs, not a file dump.** Prose pages explain the runtime model, architecture, and conventions, with flow diagrams and an *On this page* outline. Each page lists the *Relevant source files* it was written from, so every claim traces back to code.
- **An interactive knowledge graph.** The whole repository as a force-directed graph of files, classes, functions, methods, and interfaces: thousands of nodes and edges, filterable by symbol type, so you can see how the codebase actually hangs together.
- **Ask MewboWiki.** An inline Q&A box on every page. Ask a question, pick a model, and a wiki-aware sub-agent answers from the indexed repository rather than from the open web.

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
3. **Graph** the code into a property graph: parse each file's AST with tree-sitter, then lift files, classes, functions, methods, and interfaces into nodes linked by their call, import, and definition relationships.
4. **Plan** the set of pages to write.
5. **Pages:** sub-agents write each page in parallel, grounded in the scanned source.
6. **Finalize:** dedupe, attach the repository description, and publish.

The landing-page card and the indexing screen read the same progress signal, so they never disagree about which phase a run is in. When a repository changes, **Re-index this wiki** replays the pipeline and refreshes every page.

> [!NOTE] Ground the output with repo notes
> If the repository contains a `.mewbo/wiki.json` file, the indexer adopts its page plan and folds its notes into the page-writing prompts. It's the simplest way to steer which pages get written and to inject facts the code alone won't reveal.

---

## The knowledge graph

<div style="display: flex; justify-content: center;">
  <img src="../mewbo-wiki-03-graph.jpg" alt="The MewboWiki knowledge graph view rendering thousands of nodes and edges for a repository, with a legend counting files, classes, functions, methods, and interfaces" style="width: 100%; max-width: 960px; height: auto;" />
</div>

Every indexed repository ships with a **Graph** view: a live, zoomable map of the codebase built from the same index that backs the pages. Nodes are coloured by symbol type (file, class, function, method, interface) and the legend counts each. Use it to find the dense centres of a project, trace how a subsystem connects to the rest, or jump from a symbol to the page that documents it.

What you see here is the *structural* layer of the graph. Question answering runs over the same graph with a semantic memory layer added on top, described in the next section.

---

## Grounded by a code memory graph

Most documentation tools chop a repository into text and search it like prose. Mewbo builds a graph instead. As shown above, indexing lifts each file's AST into a **code property graph**: files, classes, functions, methods, and interfaces as nodes; their call, import, and definition edges as the wiring. This captures how the repository actually fits together.

An LLM pass then attaches a second layer over the same nodes: semantic links between the parts of a codebase that belong together even when no syntax connects them. Structure and meaning live as separate dimensions over one shared graph: a **multiplex code memory graph** that remembers both how the code is wired and what it means. It's what carries the wiki from single-file lookups to repository scale.

That memory is what makes answers fast and authoritative. A question doesn't pull a handful of look-alike snippets; it traverses the graph across **multiple hops** (a symbol, to its callers, to the module that owns them, to the note that explains why), assembling a connected, repository-scale context before the model writes a word. Keyword and semantic search seed the entry points; the memory graph expands them into the full picture.

> [!NOTE] Why multi-hop matters
> A flat search answers a cross-module question with three disconnected results. Traversing the memory graph follows real relationships in the code instead, so the answer holds together and every hop stays grounded. Semantic embeddings are an optional accelerator. Without them, keyword retrieval still seeds the graph and the wiki keeps working.

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

> [!NOTE] Going deeper
> The wiki runs on the same engine as everything else. See [Sub-agents](features-agents.md) for the delegation model that writes pages in parallel, and [Architecture Overview](core-orchestration.md) for the session runtime underneath.
